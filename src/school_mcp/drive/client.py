"""Scoped USTC drive API using the current official browser session."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import time
import uuid
from urllib.parse import urlsplit, parse_qs
from ..campus.session import browser_session
from ..mail.config import local_dir
from ..network import limiter, http_client, bounded_request
from ..service_errors import ServiceError

READ = {'/user/get_user_info','/repo/get_repo_list','/dir/get_dir_items_list','/file/get_file_download_link','/file/get_upload_link'}
WRITE = {'/dir/create_dir','/file/move_or_copy_item'}
MAX_FILE = 50 * 1024 * 1024


def drive_path(path):
    if not isinstance(path, str) or not path.startswith('/') or len(path) > 1000 or '\\' in path or any(ord(c)<32 for c in path) or '..' in path.split('/'):
        raise ServiceError('云盘路径须为库内绝对路径，不能含 ..。', code='invalid_arguments')
    return str(PurePosixPath(path))


def signature(params, client_id, timestamp=None):
    values = {**params, 'client_id':client_id, 'timestamp':int(time.time()) if timestamp is None else timestamp}
    def flatten(k, v):
        if v is None or v == '': return []
        if isinstance(v, bool): return [(k, '1' if v else '0')]
        if isinstance(v, list): return [pair for i,x in enumerate(v) for pair in flatten(f'{k}[{i}]', x)]
        if isinstance(v, dict): return [pair for n,x in sorted(v.items()) for pair in flatten(f'{k}[{n}]', x)]
        return [(k, str(v))]
    text = '&'.join(k+'='+v for name,value in sorted(values.items()) for k,v in flatten(name, value)).replace('\r','').replace('\n','')
    return {**values, 'sn':hashlib.md5(text.encode()).hexdigest()}


class Drive:
    def __init__(self, session, headers, client_id):
        self.session, self.headers, self.client_id = session, headers, client_id

    def call(self, endpoint, params=None, method='GET'):
        if not (method == 'GET' and endpoint in READ or method == 'POST' and endpoint in WRITE):
            raise ServiceError('云盘操作不在适配范围内。', code='invalid_arguments')
        limiter.acquire('drive')
        payload = signature(params or {}, self.client_id)
        kwargs = {'params':payload} if method=='GET' else {'data':json.dumps(payload, ensure_ascii=False).encode()}
        response = self.session.context.request.fetch(self.session.base + '/api/v1' + endpoint, method=method,
            headers={**self.headers, 'X-CSRF-Token':secrets.token_hex(16), 'Content-Type':'application/json'},
            timeout=30000, max_redirects=0, **kwargs)
        limiter.response('drive', response.status, response.headers)
        if response.status != 200 or len(response.body()) > 8*1024*1024:
            raise ServiceError('云盘没有返回可确认的结果。', code='unavailable')
        result = response.json()
        if result.get('code') != 1:
            raise ServiceError('云盘操作未成功，不能将其当作空结果。', code='authentication_required' if result.get('code') in (401,-1) else 'unavailable')
        return result.get('data')

    def repos(self):
        result = self.call('/repo/get_repo_list')
        if not isinstance(result, list):
            raise ServiceError('云盘库列表结构变化。', code='unavailable')
        return result

    def check_repo(self, repo_id, writable=False):
        if not isinstance(repo_id, str) or not re.fullmatch('[a-fA-F0-9-]{36}', repo_id):
            raise ServiceError('请使用库列表返回的 id。', code='invalid_arguments')
        repo = next((x for x in self.repos() if x['id']==repo_id), None)
        if not repo or repo.get('encrypted') or writable and repo.get('permission') != 'rw':
            raise ServiceError('该库不在当前账号可用范围，或没有所需权限。', code='invalid_arguments')
        return repo

    def listing(self, repo_id, path):
        result = self.call('/dir/get_dir_items_list', {'repo_id':repo_id, 'path':drive_path(path)})
        if not isinstance(result, list):
            raise ServiceError('文件目录结构变化。', code='unavailable')
        return result


@contextmanager
def connected():
    with browser_session('drive') as s:
        auth = {}; observed = []
        def on_request(r):
            if urlsplit(r.url).hostname == 'pan.ustc.edu.cn' and urlsplit(r.url).path == '/api/v1/user/get_user_info':
                headers = r.all_headers()
                if headers.get('x-auth-token'):
                    auth['headers'] = {'X-Auth-Token':headers['x-auth-token']}
                    auth['client_id'] = parse_qs(urlsplit(r.url).query).get('client_id', [''])[0]
        def on_response(r):
            if urlsplit(r.url).hostname == 'pan.ustc.edu.cn' and urlsplit(r.url).path == '/api/v1/user/get_user_info':
                try:
                    v = r.json()
                    if v.get('code')==1 and isinstance(v.get('data'), dict):observed.append(v['data'])
                except ValueError:pass
        s.context.on('request', on_request); s.context.on('response', on_response)
        page = s.goto()
        for _ in range(150):
            if observed and auth.get('headers') and auth.get('client_id'):break
            login = page.get_by_text('统一身份认证登录', exact=True)
            if login.count() and login.first.is_visible():
                checkbox = page.get_by_role('checkbox')
                if checkbox.count()==1 and not checkbox.is_checked():checkbox.check()
                login.first.click()
            page.wait_for_timeout(100)
        s.require_site()
        if not observed or not auth.get('headers'):
            raise ServiceError('云盘需要本人完成学校认证。', code='manual_verification_required')
        s.confirm()
        yield Drive(s, auth['headers'], auth['client_id'])


def libraries():
    with connected() as drive:
        return {'connected':True, 'libraries':[{k:x.get(k) for k in ('id','name','permission','encrypted','size')} for x in drive.repos()],
                'note':'后续查询需指定某个库和目录；不会自动遍历所有个人文件。'}


def listing(repo_id, path='/', keyword='', limit=50):
    if not isinstance(keyword, str) or len(keyword)>100 or not 1 <= limit <= 200:
        raise ServiceError('文件筛选范围无效。', code='invalid_arguments')
    with connected() as drive:
        drive.check_repo(repo_id)
        result = drive.listing(repo_id, path)
        rows = [x for x in result if not keyword or keyword.casefold() in x.get('name','').casefold()]
        return {'repo_id':repo_id, 'path':drive_path(path), 'items':[{k:x.get(k) for k in ('id','name','type','size','parent_dir','fullpath','last_modified')} for x in rows[:limit]],
                'matching_count':len(rows), 'truncated':len(rows)>limit, 'scope':'指定目录的直接子项', 'untrusted_content':True}


def transfer_url(url):
    try:
        p = urlsplit(url)
        if p.scheme=='https' and p.hostname=='pan.ustc.edu.cn' and p.port in (None,443) and not p.username and not p.password:
            return url
    except (ValueError, TypeError):pass
    raise ServiceError('文件传输地址不在学校云盘 HTTPS 范围内。', code='unavailable')


def download_bytes(drive, repo_id, path):
    link = drive.call('/file/get_file_download_link', {'repo_id':repo_id, 'path':drive_path(path)})
    url = transfer_url(link.get('download_link'))
    # Signed file URL only. Never forward the API token to a storage endpoint.
    with http_client('drive', ServiceError, timeout=30, trust_env=False, follow_redirects=False) as client:
        r = bounded_request(client, 'GET', url, maximum_bytes=MAX_FILE, error_type=ServiceError)
        if r.status_code != 200 or 'text/html' in r.headers.get('content-type',''):
            raise ServiceError('未取得云盘文件内容。', code='unavailable')
        return r.content


def download(repo_id, path):
    path = drive_path(path)
    with connected() as drive:
        drive.check_repo(repo_id)
        parent = str(PurePosixPath(path).parent); name = PurePosixPath(path).name
        item = next((x for x in drive.listing(repo_id,parent) if x.get('name')==name and x.get('type')=='file'), None)
        if not item or item.get('size', 0)>MAX_FILE:
            raise ServiceError('文件不在该目录或超过 50 MiB。', code='invalid_arguments')
        raw = download_bytes(drive, repo_id, path)
    name = 'download-' + re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)[:120]
    folder = local_dir()/'drive-downloads'/uuid.uuid4().hex; folder.mkdir(parents=True)
    output = folder/name; output.write_bytes(raw)
    return {'path':str(output.resolve()), 'filename':name, 'sha256':hashlib.sha256(raw).hexdigest(), 'size_bytes':len(raw), 'downloaded':True}
