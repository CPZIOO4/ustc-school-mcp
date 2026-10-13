"""Student-affairs scholarship reads, without application form submissions."""
import hashlib
import re
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from ..campus.session import browser_session, checked_url
from ..desktop.storage import read_private, write_private
from ..mail.config import local_dir
from ..service_errors import ServiceError

PAGES = {'open': '/jxj/scholarship/pbApplyMain.aspx', 'applied': '/jxj/scholarship/pdHasAppliedMain.aspx',
         'awarded': '/jxj/scholarship/pdSuccessItemMain.aspx'}
READ_PATHS = {'/jxj/scholarship/xgItemMainContentRead.aspx', '/usp/home/infoNoticeRead.aspx', '/usp/home/infoManageCardRead.aspx'}


def connected(s):
    page = s.goto()
    page.get_by_text('奖学金申报', exact=True).first.wait_for(state='visible', timeout=12000)
    # Main school page is reached solely through this account's SSO state.
    # A session saved for another identity is rejected before browser creation.
    s.confirm()
    return page


def check():
    with browser_session('student') as s:
        connected(s)
        return {'connected': True, 'read_only': True}


def save_refs(s, links):
    path = local_dir() / 'student-read-references.dpapi'
    previous = read_private(path, {})
    refs = previous.get('refs', {}) if previous.get('account') == s.account else {}
    result = []
    for title, url in links:
        checked_url('student', url)
        if urlsplit(url).path not in READ_PATHS:
            continue
        ref = hashlib.sha256(url.encode()).hexdigest()[:24]
        refs[ref] = {'url': url, 'title': title}
        if not any(x['reference'] == ref for x in result):
            result.append({'reference': ref, 'title': title})
    write_private(path, {'account': s.account, 'refs': dict(list(refs.items())[-500:])})
    return result


def enter_scholarships(s, home):
    with s.context.expect_page() as opened:
        home.get_by_text('奖学金申报', exact=True).first.click()
    s.page = opened.value
    s.page.wait_for_load_state('domcontentloaded')
    s.page.wait_for_url('**/jxj/scholarship/pbApplyMain.aspx', timeout=20000)


def scholarships(kind='open', keyword=''):
    if kind not in PAGES or len(keyword) > 100:
        raise ServiceError('奖学金查询类型或关键词无效。', code='invalid_arguments')
    with browser_session('student') as s:
        home = connected(s)
        # The homepage action establishes a separate scholarship-module session.
        # Deep-linking directly to the list returns the module's timeout page.
        enter_scholarships(s, home)
        page = s.page if kind == 'open' else s.goto(PAGES[kind])
        # This legacy page loads its list into the DOM after document load.
        page.locator('#oHtmlTable').wait_for(state='attached', timeout=12000)
        soup = BeautifulSoup(page.content(), 'html.parser')
        table = soup.select_one('#oHtmlTable')
        rows = []
        for tr in table.select('tr'):
            if 'display:none' in (tr.get('style') or '').replace(' ', '').lower():
                continue
            cells = tr.find_all(['td', 'th'], recursive=False)
            cells = [c for c in cells if 'display:none' not in (c.get('style') or '').replace(' ', '').lower()]
            texts = [c.get_text(' ', strip=True) for c in cells]
            if len(texts) < 2 or not re.fullmatch(r'\d+', texts[0]):
                continue
            if keyword and keyword.casefold() not in ' '.join(texts).casefold():
                continue
            links = [(a.get_text(' ', strip=True), urljoin(page.url, a['href'])) for a in tr.select('a[href]')]
            from .applications import remember
            rows.append({'number': texts[0], 'name': texts[1], 'fields': texts[2:], 'details': save_refs(s, links),
                         **remember(s.account, texts[1], texts[2:], links)})
        return {'kind': kind, 'items': rows, 'count': len(rows), 'source_url': s.base + PAGES[kind],
                'untrusted_content': True, 'submitted': False,
                'note': '仅查询本平台记录；获奖结果与申请状态分开。申请链接不会被自动打开或提交。'}


def notices(keyword='', limit=20):
    if len(keyword) > 100 or not 1 <= limit <= 50:
        raise ServiceError('通知查询范围无效。', code='invalid_arguments')
    with browser_session('student') as s:
        page = connected(s)
        soup = BeautifulSoup(page.content(), 'html.parser')
        links = []
        for a in soup.select('a[href]'):
            title = a.get_text(' ', strip=True)
            url = urljoin(page.url, a['href'])
            if title and urlsplit(url).path in READ_PATHS and (not keyword or keyword in title):
                links.append((title, url))
        refs = save_refs(s, list(dict.fromkeys(links)))
        return {'items': refs[:limit], 'truncated': len(refs) > limit, 'scope': '学工首页当前可见通知', 'untrusted_content': True}


def read(reference):
    if not re.fullmatch('[a-f0-9]{24}', reference):
        raise ServiceError('请使用列表返回的 reference。', code='invalid_arguments')
    with browser_session('student') as s:
        refs = read_private(local_dir() / 'student-read-references.dpapi', {})
        item = refs.get('refs', {}).get(reference) if refs.get('account') == s.account else None
        if not item or urlsplit(item['url']).path not in READ_PATHS:
            raise ServiceError('该详情未由当前账号的列表发现，请重新查询。', code='invalid_arguments')
        home = connected(s)
        if urlsplit(item['url']).path.startswith('/jxj/'):
            enter_scholarships(s, home)
        page = s.goto(item['url'])
        text = page.locator('body').inner_text(timeout=10000)
        # Attachments are references only; the download adapter enforces its own scope.
        links = page.locator('a[href]').evaluate_all('(els)=>els.map(e=>({name:e.innerText,url:e.href}))')
        safe = [{'name':x['name'], 'url':x['url']} for x in links if urlsplit(x['url']).hostname == 'xgyth.ustc.edu.cn'
                and urlsplit(x['url']).path.lower().endswith(('.pdf','.doc','.docx','.xls','.xlsx')) and not urlsplit(x['url']).query]
        return {'reference': reference, 'title': item['title'], 'content': text[:22000], 'truncated': len(text) > 22000,
                'attachments': safe, 'untrusted_content': True, 'submitted': False,
                'next_step': '按原文逐项核对资格、评审学年、截止时间和材料；缺少事实需问用户。可用本机文件工具准备草稿，不提交申请。'}
