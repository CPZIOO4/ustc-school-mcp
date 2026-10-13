"""Restricted public campus pages and attachments, without arbitrary actions."""
import hashlib
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urljoin, unquote
from playwright.sync_api import sync_playwright
from ..browser import chrome_browser
from ..desktop.storage import read_private, write_private
from ..mail.config import local_dir
from ..network import http_client, bounded_request, limiter
from ..service_errors import ServiceError

HOSTS = {'ustc.life', 'www.teach.ustc.edu.cn', 'finance.ustc.edu.cn', 'vlab.ustc.edu.cn', 'llm.ustc.edu.cn'}
SUFFIXES = {'.pdf', '.docx', '.xlsx', '.doc', '.xls', '.png', '.jpg', '.jpeg', '.txt', '.zip'}
ARTICLE = r'/(?:notice|education|service|calendar)(?:/[a-z][a-z0-9-]*)*/[1-9][0-9]*\.html'


def attachment_page(p):
    """Only the official WordPress attachment view, never arbitrary query actions."""
    return p.hostname == 'www.teach.ustc.edu.cn' and (
        p.path == '/' and re.fullmatch(r'attachment_id=[1-9][0-9]{0,9}', p.query) is not None or
        not p.query and re.fullmatch(ARTICLE + r'/attachment/[^/?#]{1,300}/?', unquote(p.path)) is not None)


def safe_url(url, attachment=False):
    try:
        p = urlsplit(url)
        path = unquote(p.path)
        valid = (p.scheme == 'https' and p.hostname in HOSTS and p.port in (None, 443)
                 and not p.username and not p.password and (not p.query or attachment_page(p)) and '\\' not in path
                 and '..' not in path.split('/') and not any(ord(c) < 32 for c in path))
        if attachment:
            valid = valid and (attachment_page(p) or Path(path).suffix.lower() in SUFFIXES and not p.query) and p.hostname in {'www.teach.ustc.edu.cn','finance.ustc.edu.cn'}
        else:
            valid = valid and ((p.hostname == 'ustc.life' and path in ('', '/'))
                or p.hostname == 'www.teach.ustc.edu.cn' and (re.fullmatch(ARTICLE, path) or attachment_page(p))
                or p.hostname == 'finance.ustc.edu.cn' and re.fullmatch(r'/\d{4}/\d{4}/c\d+a\d+/page\.psp', path)
                or p.hostname in {'vlab.ustc.edu.cn','llm.ustc.edu.cn'} and path.startswith(('/docs/', '/guide/')))
        if valid:
            return p._replace(fragment='').geturl()
    except (ValueError, TypeError):
        pass
    raise ServiceError('仅支持已适配的学校公开正文、导航和官方帮助页面。登录业务请使用对应平台工具。', code='invalid_arguments')


def read(url, keyword='', offset=0, limit=12000):
    url = safe_url(url)
    if not isinstance(keyword, str) or len(keyword) > 100 or not 0 <= offset <= 500000 or not 1 <= limit <= 16000:
        raise ServiceError('页面读取范围无效。', code='invalid_arguments')
    limiter.acquire('web')
    with sync_playwright() as pw, chrome_browser(pw, headless=True) as browser:
        context = browser.new_context(service_workers='block', accept_downloads=False)
        host = urlsplit(url).hostname
        def guard(route):
            r = route.request; p = urlsplit(r.url)
            if r.method not in {'GET','HEAD'} or p.scheme != 'https' or p.hostname != host:
                return route.abort()
            if r.is_navigation_request():
                try:
                    safe_url(r.url)
                except ServiceError:
                    return route.abort()
            return route.continue_()
        context.route('**/*', guard)
        page = context.new_page()
        response = page.goto(url, wait_until='domcontentloaded', timeout=30000)
        if response:
            limiter.response('web', response.status, response.headers)
            if response.status != 200:
                raise ServiceError('公开页面暂不可读取。', code='unavailable')
        safe_url(page.url)
        text = page.locator('body').inner_text(timeout=10000)
        links = page.locator('a[href]').evaluate_all('(xs)=>xs.map(e=>({name:e.innerText,url:e.href}))')
        title = page.title()
        context.close()
    refs = read_private(local_dir() / 'public-web-references.dpapi', {})
    items = []
    for x in links:
        if keyword and keyword.casefold() not in x['name'].casefold():
            continue
        p = urlsplit(x['url'])
        if p.scheme not in {'https','http'} or p.username or p.password or p.query and not attachment_page(p):
            continue
        item = {'name': x['name'][:180], 'url': x['url']}
        try:
            target = safe_url(x['url'], attachment=True)
            ref = hashlib.sha256(target.encode()).hexdigest()[:24]
            refs[ref] = target
            item['download_reference'] = ref
        except ServiceError:
            pass
        items.append(item)
    write_private(local_dir() / 'public-web-references.dpapi', dict(list(refs.items())[-500:]))
    if keyword:
        text = '\n'.join(line for line in text.splitlines() if keyword.casefold() in line.casefold())
    end = min(offset + limit, len(text))
    limiter.success('web')
    return {'title': title, 'source_url': url, 'content': text[offset:end], 'next_offset': end if end < len(text) else None,
            'complete': end >= len(text), 'links': items[:100], 'links_truncated': len(items) > 100,
            'scope': 'public_page_only', 'untrusted_content': True, 'keyword_filter': keyword or None}


def download(reference):
    if not isinstance(reference, str) or not re.fullmatch('[a-f0-9]{24}', reference):
        raise ServiceError('请使用页面返回的下载引用。', code='invalid_arguments')
    url = read_private(local_dir() / 'public-web-references.dpapi', {}).get(reference)
    if not url:
        raise ServiceError('下载引用失效，请重新读取正文。', code='invalid_arguments')
    url = safe_url(url, attachment=True)
    with http_client('web', ServiceError, timeout=30, follow_redirects=False, trust_env=False) as client:
        for _ in range(6):
            result = bounded_request(client, 'GET', url, maximum_bytes=50*1024*1024, error_type=ServiceError)
            if result.status_code in {301,302,303,307,308}:
                next_url = safe_url(urljoin(url, result.headers.get('location','')), attachment=True)
                if urlsplit(next_url).hostname != urlsplit(url).hostname:
                    raise ServiceError('附件跳转到了其他站点，已停止。', code='unavailable')
                url = next_url
                continue
            if result.status_code == 200 and 'text/html' in result.headers.get('content-type', '') and attachment_page(urlsplit(url)):
                from bs4 import BeautifulSoup
                if len(result.content) > 2 * 1024 * 1024:
                    raise ServiceError('附件说明页过大，已停止。', code='unavailable')
                soup = BeautifulSoup(result.content, 'html.parser')
                targets = set()
                for link in soup.select('main article.attachment a[href]'):
                    target = urljoin(url, link['href'])
                    try:
                        safe_url(target, attachment=True)
                    except ServiceError:
                        continue
                    if urlsplit(target).hostname == urlsplit(url).hostname and not attachment_page(urlsplit(target)):
                        targets.add(target)
                if len(targets) != 1:
                    raise ServiceError('附件说明页没有唯一可核实的文件链接。', code='unavailable')
                url = targets.pop()
                continue
            if result.status_code != 200 or 'text/html' in result.headers.get('content-type', ''):
                raise ServiceError('没有取得附件文件。', code='unavailable')
            raw = result.content
            break
        else:
            raise ServiceError('附件重定向过多。', code='unavailable')
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', unquote(urlsplit(url).path).split('/')[-1])[:160]
    filename = 'download-' + filename
    folder = local_dir() / 'web-downloads' / uuid.uuid4().hex; folder.mkdir(parents=True)
    path = folder / filename; path.write_bytes(raw)
    return {'downloaded': True, 'path': str(path.resolve()), 'filename': filename, 'sha256': hashlib.sha256(raw).hexdigest(),
            'size_bytes': len(raw), 'source_url': url, 'note': '下载到本机；未打开或运行附件。'}
