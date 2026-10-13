"""User-operated official login windows; passwords are never captured by the app."""
import re
import time
from urllib.parse import urlsplit

from ..bb.identity import (IDENTITY_HOSTS, load_credentials, load_device_state, save_device_state,
                           save_session_identity)
from ..bb.login import CONTEXT_OPTIONS, remember_device, sso_url_from_html, gateway_login_url, find_sso_url
from ..bb.session import BASE_URL, PORTAL_PATH, bb_cookies, save_session, BBError
from ..bb.client import BBClient
from ..browser import chrome_browser

CAPTURE_ACCOUNT = """(() => {
  if (!['id.ustc.edu.cn','passport.ustc.edu.cn'].includes(location.hostname) || location.protocol !== 'https:') return;
  const report = () => {
    const e = document.querySelector('input[name="username"],input#username,input[placeholder*="学工号"],input[placeholder*="GID"]');
    if(e && e.value) window.schoolLoginAccount(e.value).catch(()=>{});
  };
  document.addEventListener('input', report, true);
  document.addEventListener('change', report, true);
  document.addEventListener('submit', report, true);
})();"""


def manual_identity(notify, timeout=600):
    from . import native_core
    if native_core.enabled():
        from .login_native import run
        return run('identity', notify, timeout=timeout)
    from playwright.sync_api import sync_playwright
    previous = None
    try:
        previous = load_credentials()['username']
    except BBError:
        pass
    observed = {'account': previous}

    def capture(source, value):
        p = urlsplit(source['frame'].url)
        if p.scheme == 'https' and p.hostname in IDENTITY_HOSTS and p.port in (443, None):
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}', value):
                observed['account'] = value

    notify('waiting_for_user', '请在学校官方窗口登录并完成验证；应用不读取或保存登录密码。')
    with sync_playwright() as p, chrome_browser(p, headless=False) as browser:
        options = dict(CONTEXT_OPTIONS)
        options['user_agent'] = f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{browser.version} Safari/537.36'
        state = None
        if previous:
            try:
                state = load_device_state(previous)['state']
            except BBError:
                pass
        context = browser.new_context(**options, storage_state=state, service_workers='block', accept_downloads=False)
        context.expose_binding('schoolLoginAccount', capture)
        context.add_init_script(CAPTURE_ACCOUNT)
        page = context.new_page()
        page.goto(find_sso_url() or BASE_URL + PORTAL_PATH, wait_until='domcontentloaded', timeout=45000)
        deadline, next_check = time.monotonic() + timeout, 0
        followed = set()
        while browser.is_connected() and context.pages and time.monotonic() < deadline:
            page = context.pages[-1]
            url = urlsplit(page.url)
            if url.hostname in IDENTITY_HOSTS:
                remember_device(page)
            elif url.hostname in {'bb.ustc.edu.cn', 'www.bb.ustc.edu.cn'}:
                target = gateway_login_url(page.url, page.content()) or sso_url_from_html(page.url, page.content())
                if target and target not in followed:
                    followed.add(target)
                    page.goto(target, wait_until='domcontentloaded', timeout=45000)
                    continue
                if url.path == PORTAL_PATH.split('?')[0] and time.monotonic() >= next_check:
                    next_check = time.monotonic() + 5
                    cookies = bb_cookies(context.cookies())
                    try:
                        verified = BBClient(cookies=cookies).check()
                    except BBError:
                        verified = None
                    if verified and verified.get('connected'):
                        account = observed['account']
                        if not account:
                            raise ValueError('已到达学校页面，但未识别本次账号；请重新登录。')
                        if previous and previous != account:
                            raise ValueError('登录了另一个账号；请先使用“切换账号”清理旧会话。')
                        save_session_identity(account)
                        save_session(cookies)
                        save_device_state(account, context.storage_state(indexed_db=True), 'chrome', options)
                        notify('connected', '统一身份及 BB 登录已核实，会话与设备信任已加密保存。')
                        return {'connected': True, 'account': account, 'password_saved': False}
            page.wait_for_timeout(300)
    raise ValueError('登录窗口已关闭或等待超时，尚未确认登录成功。')


def manual_icourse(notify, timeout=600):
    from . import native_core
    if native_core.enabled():
        from .login_native import run
        return run('icourse', notify, timeout=timeout)
    from playwright.sync_api import sync_playwright
    from ..icourse.session import save_session as save_icourse, load_session as load_icourse
    notify('waiting_for_user', '请在评课社区官方页面登录；这是独立账号。')
    with sync_playwright() as p, chrome_browser(p, headless=False) as browser:
        context = browser.new_context(service_workers='block', accept_downloads=False)
        try:
            context.add_cookies(load_icourse()['cookies'])
        except Exception:
            pass
        page = context.new_page()
        page.goto('https://icourse.club/signin/', wait_until='domcontentloaded', timeout=45000)
        deadline = time.monotonic() + timeout
        while browser.is_connected() and context.pages and time.monotonic() < deadline:
            page = context.pages[-1]
            parsed = urlsplit(page.url)
            if parsed.scheme == 'https' and parsed.netloc == 'icourse.club':
                hrefs = page.locator('a').evaluate_all("els=>els.filter(e=>e.textContent.trim()==='个人主页').map(e=>e.getAttribute('href'))")
                ids = {m[1] for h in hrefs if (m := re.fullmatch(r'/user/(\d+)', h or ''))}
                if len(ids) == 1:
                    save_icourse(context.cookies(), ids.pop(), page.evaluate('navigator.userAgent'))
                    return {'connected': True, 'password_saved': False}
            page.wait_for_timeout(500)
    raise ValueError('评课社区登录未完成。')


def manual_service(service, notify, stopped=None, timeout=600):
    """Explicit user-operated login; only verified site sessions reach disk."""
    from . import native_core
    if native_core.enabled():
        from .login_native import run
        return run(service, notify, stopped, timeout)
    import importlib
    from playwright.sync_api import sync_playwright, Error as PlaywrightError
    from ..service_errors import ServiceError
    if service == 'bb':
        return manual_identity(notify, timeout)
    if service not in {'jw', 'young', 'finance', 'library', 'nan7'}:
        raise ValueError('此平台没有需要登录的个人入口。')
    account = load_credentials()['username']
    module = importlib.import_module(f'school_mcp.{service}.session')
    try:
        device = load_device_state(account)
        state = device['state']
    except BBError:
        state = {'cookies': [], 'origins': []}
    try:
        previous = module.load_session()
        own = previous.get('state', {'cookies': previous.get('cookies', []), 'origins': []})
        cookies = {(c['name'], c['domain'], c['path']): c for c in state['cookies'] + own['cookies']}
        origins = {o['origin']: o for o in state['origins'] + own['origins']}
        state = {'cookies': list(cookies.values()), 'origins': list(origins.values())}
    except ServiceError:
        pass
    observed = {'account': account}
    def capture(source, value):
        url = urlsplit(source['frame'].url)
        if url.scheme == 'https' and url.hostname in IDENTITY_HOSTS and url.port in (None, 443):
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}', value):
                observed['account'] = value
    entries = {'jw': 'https://jw.ustc.edu.cn/ucas-sso/login',
               'young': 'https://young.ustc.edu.cn/login/sc-wisdom-group-learning/dataAnalysis/visual'}
    if service == 'library':
        from ..library.login import find_sso_url as library_entry
        entry = library_entry()
    else:
        entry = entries.get(service) or (module.LOGIN_URL if service == 'nan7' else module.HOME)
    notify('waiting_for_user', '请在该平台官方窗口完成登录和验证，完成后会自动保存会话。')
    with sync_playwright() as pw, chrome_browser(pw, headless=False) as browser:
        options = dict(CONTEXT_OPTIONS)
        options['user_agent'] = f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{browser.version} Safari/537.36'
        context = browser.new_context(**options, storage_state=state, service_workers='block', accept_downloads=False)
        context.expose_binding('schoolLoginAccount', capture)
        context.add_init_script(CAPTURE_ACCOUNT)
        pending = []
        if service == 'finance':
            from ..finance.client import install_guard
            pending, _ = install_guard(context, login=True)
        page = context.new_page()
        try:
            page.goto(entry, wait_until='domcontentloaded', timeout=45000)
        except PlaywrightError:
            if not pending:
                raise ValueError('官方登录页面未打开，请检查网络后重试。') from None
        deadline, next_check = time.monotonic() + timeout, 0
        while browser.is_connected() and context.pages and time.monotonic() < deadline:
            if stopped and stopped.is_set():
                raise ValueError('已停止登录，之前保存的会话保留。')
            page = context.pages[-1]
            if pending:
                page.goto(pending.pop(0), wait_until='domcontentloaded', timeout=45000)
            url = urlsplit(page.url)
            if url.hostname not in IDENTITY_HOSTS and observed['account'].casefold() != account.casefold():
                raise ValueError('本次输入账号与学校身份不同；请先切换账号，不保存混用的会话。')
            if url.hostname in IDENTITY_HOSTS:
                remember_device(page)
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + 4
                try:
                    saved = save_verified_service(service, module, context, page, account, options['user_agent'])
                except ServiceError as exc:
                    # Authentication can be incomplete while the user is typing. Network/rate failures stop.
                    if getattr(exc, 'code', '') not in {'authentication_required', 'identity_required'}:
                        raise
                    saved = False
                if saved:
                    save_device_state(account, context.storage_state(indexed_db=True), 'chrome', options)
                    return {'connected': True, 'service': service, 'password_saved': False}
            page.wait_for_timeout(500)
    raise ValueError('登录窗口已关闭或等待超时；已有会话保留。')


def save_verified_service(service, module, context, page, account, user_agent):
    """No save before a site-specific authenticated response or page is verified."""
    url = urlsplit(page.url)
    if service in {'jw', 'library'}:
        if url.hostname != module.HOST or url.path.endswith(('login-cas.php', '/login')):
            return False
        cookies = context.cookies()
        if service == 'jw':
            if url.scheme != 'https' or url.path != '/home':
                return False
            from ..jw.client import JWClient
            result = JWClient(cookies=module.jw_cookies(cookies), user_agent=user_agent).check()
            if result.get('connected'):
                module.save_session(cookies, user_agent)
                return True
        else:
            if url.scheme not in {'http', 'https'} or url.path not in {'/reader/redr_info.php', '/reader/book_lst.php', '/reader/book_hist.php'}:
                return False
            from ..library.client import LibraryClient
            base = f'{url.scheme}://{module.HOST}'
            result = LibraryClient(module.library_cookies(cookies), base, user_agent).check()
            if result.get('connected'):
                module.save_session(cookies, base, user_agent, account)
                return True
    elif service in {'young', 'finance'}:
        if url.scheme != 'https' or url.hostname != module.HOST:
            return False
        if service == 'young':
            from ..young.client import authenticated_page
            valid = authenticated_page(page)
        else:
            from ..finance.client import authenticated_portal, smart_entry
            valid = authenticated_portal(page)
            if valid:
                smart_entry(page.content())
        if valid:
            module.save_session(context.storage_state(indexed_db=True), account, user_agent)
            return True
    elif service == 'nan7':
        from ..nan7.login import is_market_origin
        if is_market_origin(page.url):
            token = page.evaluate("localStorage.getItem('token')")
            if module.valid_token(token):
                from ..nan7.client import Nan7Client
                if Nan7Client(token).check().get('connected'):
                    module.save_session(token, account)
                    return True
    return False
