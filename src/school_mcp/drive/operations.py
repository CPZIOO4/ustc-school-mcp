"""Prepare, execute once, and read back scoped drive writes."""
import hashlib
from pathlib import Path, PurePosixPath
import re
from ..bb.identity import load_credentials
from ..workflow_store import PlanStore, digest
from ..service_errors import ServiceError
from ..network import limiter
from .client import connected, drive_path, transfer_url, download_bytes, MAX_FILE


def store():
    return PlanStore('drive', digest(load_credentials()['username']))


def name_ok(name):
    return isinstance(name,str) and 1<=len(name)<=180 and name not in {'.','..'} and not re.search(r'[\\/\x00-\x1f]',name)


def prepare(operation, repo_id, path='/', name='', destination='', files=None):
    if operation not in {'mkdir','upload','move'}:
        raise ServiceError('支持 mkdir、upload、move。', code='invalid_arguments')
    path=drive_path(path); files=files or []; manifests=[]
    if operation!='upload' and files or operation=='upload' and not 1<=len(files)<=5:
        raise ServiceError('只有上传操作可带文件，每次 1–5 个。', code='invalid_arguments')
    if operation in {'mkdir','move'} and not name_ok(name):
        raise ServiceError('请提供一个有效的文件或文件夹名称。', code='invalid_arguments')
    if operation=='move':
        destination=drive_path(destination)
        source=str(PurePosixPath(path)/name)
        if destination==path or destination==source or destination.startswith(source+'/'):
            raise ServiceError('移动目标不能是原目录或源文件夹的子目录。', code='invalid_arguments')
    for f in files:
        p=Path(f).resolve()
        if not p.is_file() or p.stat().st_size>MAX_FILE or not name_ok(p.name):
            raise ServiceError('上传文件不存在或过大。', code='invalid_arguments')
        manifests.append({'path':str(p), 'name':p.name, 'size':p.stat().st_size, 'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
    if sum(f['size'] for f in manifests)>MAX_FILE or len({f['name'] for f in manifests})!=len(manifests):
        raise ServiceError('上传总量超过 50 MiB 或存在同名文件。', code='invalid_arguments')
    with connected() as drive:
        repo=drive.check_repo(repo_id, writable=True); current=drive.listing(repo_id,path)
        snapshot={}
        if operation in {'mkdir','upload'}:
            names={name} if operation=='mkdir' else {f['name'] for f in manifests}
            if names & {x['name'] for x in current}:
                raise ServiceError('目标目录已有同名项；此工具不会覆盖，请换名称或目录。', code='invalid_arguments')
        else:
            item=next((x for x in current if x['name']==name),None)
            if not item:
                raise ServiceError('源项目不在指定目录中。', code='invalid_arguments')
            if name in {x['name'] for x in drive.listing(repo_id,destination)}:
                raise ServiceError('目标目录已有同名项。', code='invalid_arguments')
            snapshot={k:item.get(k) for k in ('id','name','type','size')}
    content={'operation':operation,'repo_id':repo_id,'path':path,'name':name,'destination':destination,'files':manifests}
    preview={'operation':operation,'library':repo['name'],'path':path,'name':name,'destination':destination,
             'files':[{'name':f['name'],'size_bytes':f['size']} for f in manifests], 'overwrite':False}
    return store().create(digest(content),content,snapshot,{},preview)


def validate_current(drive, item):
    c=item['plan']['content']; drive.check_repo(c['repo_id'],writable=True)
    rows=drive.listing(c['repo_id'],c['path'])
    if c['operation']=='move':
        original=next((x for x in rows if x['name']==c['name']),None)
        if not original or original.get('id')!=item['plan']['snapshot'].get('id'):
            raise ServiceError('源项目已变化，请重新准备。',code='invalid_arguments')
        rows=drive.listing(c['repo_id'],c['destination'])
    names={c['name']} if c['operation']!='upload' else {f['name'] for f in c['files']}
    if names & {x['name'] for x in rows}:
        raise ServiceError('目标目录出现同名项，停止以免覆盖。',code='invalid_arguments')


def execute(plan_id,content_sha256,authorized=False):
    if authorized is not True:
        raise ServiceError('需要对库、目录、材料和操作的明确授权。',code='authorization_required')
    saved=store();item=saved.get(plan_id);c=item['plan']['content']
    if item['state']!='ready':return saved.view(plan_id)
    for f in c['files']:
        p=Path(f['path'])
        if not p.is_file() or p.stat().st_size!=f['size'] or hashlib.sha256(p.read_bytes()).hexdigest()!=f['sha256']:
            raise ServiceError('上传材料已经变化。',code='invalid_arguments')
    with connected() as drive:
        validate_current(drive,item)
        if not saved.claim(plan_id,content_sha256):return saved.view(plan_id)
        try:
            if c['operation']=='mkdir':
                drive.call('/dir/create_dir',{'repo_id':c['repo_id'],'path':c['path'],'name':c['name']},'POST')
            elif c['operation']=='move':
                drive.call('/file/move_or_copy_item',{'operate_type':'move','src_repo_id':c['repo_id'],
                    'src_parent_dir':c['path'],'src_dirents':[c['name']],'dst_repo_id':c['repo_id'],'dst_parent_dir':c['destination']},'POST')
            else:
                for index,f in enumerate(c['files']):
                    saved.phase(plan_id,'upload_'+str(index))
                    raw=Path(f['path']).read_bytes()
                    if hashlib.sha256(raw).hexdigest()!=f['sha256']:raise ValueError('changed input')
                    link=drive.call('/file/get_upload_link',{'repo_id':c['repo_id'],'path':c['path']})
                    url=transfer_url(link.get('upload_link'));limiter.acquire('drive')
                    response=drive.session.context.request.post(url,multipart={'parent_dir':c['path'],
                        'file':{'name':f['name'],'mimeType':'application/octet-stream','buffer':raw}},timeout=60000,max_redirects=0)
                    limiter.response('drive',response.status,response.headers)
                    if response.status not in (200,201):raise ServiceError('上传结果未确认。',code='unavailable')
            saved.checkpoint(plan_id,{'request_returned':True})
        except Exception:
            saved.finish(plan_id,'uncertain',{'note':'请求结果未确认，只核对原计划，不重复移动或上传。'})
            return saved.view(plan_id)
        return verify_with(drive,saved,plan_id)


def verify_with(drive,saved,plan_id):
    item=saved.get(plan_id);c=item['plan']['content'];drive.check_repo(c['repo_id'])
    target=c['destination'] if c['operation']=='move' else c['path'];rows=drive.listing(c['repo_id'],target)
    verified=False
    if c['operation']=='mkdir':
        verified=any(x['name']==c['name'] and x['type']=='dir' for x in rows)
    elif c['operation']=='move':
        moved=next((x for x in rows if x['name']==c['name']),None)
        source=drive.listing(c['repo_id'],c['path'])
        verified=bool(moved and moved.get('id')==item['plan']['snapshot'].get('id') and not any(x['name']==c['name'] for x in source))
    else:
        verified=True
        for f in c['files']:
            match=next((x for x in rows if x['name']==f['name'] and x['type']=='file'),None)
            if not match:
                verified=False;break
            raw=download_bytes(drive,c['repo_id'],str(PurePosixPath(target)/f['name']))
            if hashlib.sha256(raw).hexdigest()!=f['sha256']:
                verified=False;break
    saved.finish(plan_id,'verified' if verified else 'uncertain',{'verified':verified,'operation':c['operation'],
        'note':'已从云盘重新读取并核对结果。' if verified else '读取尚未证明操作成功；只继续核对，不重发。'})
    return saved.view(plan_id)


def status(plan_id,verify=True):
    saved=store();item=saved.get(plan_id)
    if not verify or item['state'] not in {'executing','uncertain'}:return saved.view(plan_id)
    with connected() as drive:return verify_with(drive,saved,plan_id)
