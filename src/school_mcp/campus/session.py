"""Background official-site sessions; no arbitrary browser commands or credentials in tools."""
from contextlib import contextmanager
from urllib.parse import urlsplit
import time

from ..bb.identity import load_credentials, load_device_state, save_device_state, IDENTITY_HOSTS
from ..bb.session import BBError
from ..browser import chrome_browser
from ..desktop.storage import read_private, write_private
from ..mail.config import local_dir
from ..network import limiter, PolicyError
from ..service_errors import ServiceError, policy_error
from .. import session_native as native

ENTRIES = {
    'portal': 'https://i.ustc.edu.cn/page/site/index',
    'student': 'https://xgyth.ustc.edu.cn/',
    'spaces': 'https://ic.lib.ustc.edu.cn/',
    'rooms': 'https://roombooking.cmet.ustc.edu.cn/',
    'hightech': 'https://hs.lib.ustc.edu.cn/',
    'drive': 'https://pan.ustc.edu.cn/profile/index',
    'vlab': 'https://vlab.ustc.edu.cn/vm/',
    'llm': 'https://llm.ustc.edu.cn/',
}


def checked_url(site, url):
    try:
        p = urlsplit(url)
        if p.scheme == 'https' and p.hostname == urlsplit(ENTRIES[site]).hostname and p.port in (None, 443) and not p.username and not p.password:
            return url
    except (ValueError, KeyError):
        pass
    raise ServiceError('链接不是当前平台的 HTTPS 地址。', code='invalid_arguments')


def scoped(site, state):
    if native.enabled():
        return native.invoke('sessions_filter', site, ServiceError, state=state)
    host = urlsplit(ENTRIES[site]).hostname
    hosts = IDENTITY_HOSTS | {host}
    return {'cookies': [c for c in state.get('cookies', []) if c.get('domain', '').lstrip('.') in hosts | {'ustc.edu.cn'}],
            'origins': [o for o in state.get('origins', []) if urlsplit(o['origin']).hostname in hosts]}


def session_path(site):
    if site not in ENTRIES:
        raise ServiceError('未知平台。', code='invalid_arguments')
    return local_dir() / (site + '-campus-session.dpapi')


def initial_state(site, account):
    try:
        shared = load_device_state(account)['state']
    except BBError:
        shared = {'cookies': [], 'origins': []}
    own = read_private(session_path(site), {})
    if native.enabled() and own:
        own = native.invoke('sessions_validate', site, ServiceError, value=own, account=account)
    if own and own.get('account') != account:
        raise ServiceError('平台会话与当前学校账号不同，请重新登录。', code='identity_mismatch')
    own = own.get('state', {'cookies': [], 'origins': []})
    if native.enabled():
        return native.invoke('sessions_merge', site, ServiceError, previous=own, state=shared)
    # A newly verified shared identity replaces an older site's CAS cookies.
    # Shared state is identity-scoped; service cookies stay site-local.
    cookies = {(c['name'], c['domain'], c['path']): c for c in own['cookies'] + shared['cookies']}
    origins = {o['origin']: o for o in own['origins'] + shared['origins']}
    return scoped(site, {'cookies': list(cookies.values()), 'origins': list(origins.values())})


class Session:
    def __init__(self, site, context, account):
        self.site, self.context, self.account = site, context, account
        self.base = 'https://' + urlsplit(ENTRIES[site]).hostname
        self.page = context.new_page()
        self.verified = False

    def goto(self, path=None):
        url = checked_url(self.site, self.base + path if path and path.startswith('/') else path or ENTRIES[self.site])
        limiter.acquire(self.site)
        response = self.page.goto(url, wait_until='domcontentloaded', timeout=35000)
        if response:
            limiter.response(self.site, response.status, response.headers)
        self.page.wait_for_timeout(500)
        self.require_site()
        return self.page

    def require_site(self):
        if urlsplit(self.page.url).hostname in IDENTITY_HOSTS:
            raise ServiceError('学校会话需要重新验证，请在账号页登录后继续原任务。', code='manual_verification_required')
        checked_url(self.site, self.page.url)

    def get(self, path, params=None):
        # Callers supply hard-coded read endpoints. No model-controlled methods.
        url = checked_url(self.site, self.base + path)
        limiter.acquire(self.site)
        response = self.context.request.get(url, params=params, timeout=30000, max_redirects=0)
        limiter.response(self.site, response.status, response.headers)
        if response.status in (301, 302, 303, 307, 308, 401):
            raise ServiceError('平台登录状态已失效。', code='authentication_required')
        if response.status != 200:
            raise ServiceError('平台未返回可读取的结果。', code='unavailable')
        if len(response.body()) > 8 * 1024 * 1024:
            raise ServiceError('返回内容过大，请缩小范围。', code='unavailable')
        return response

    def confirm(self, account=None):
        if account is not None and str(account).casefold() != self.account.casefold():
            raise ServiceError('平台账号与当前学校账号不一致。', code='identity_mismatch')
        self.verified = True


@contextmanager
def browser_session(site, *, headed=False):
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
    try:
        account = load_credentials()['username']
    except BBError:
        raise ServiceError('请先登录学校统一身份。', code='identity_required') from None
    try:
        limiter.acquire(site)
        with sync_playwright() as pw, chrome_browser(pw, headless=not headed) as browser:
            context = browser.new_context(storage_state=initial_state(site, account), service_workers='block', accept_downloads=False)
            # Only navigation among this service and the official identity providers.
            allowed = IDENTITY_HOSTS | {urlsplit(ENTRIES[site]).hostname}
            def guard(route):
                r = route.request
                target = urlsplit(r.url)
                if r.is_navigation_request() and target.scheme == 'http' and target.hostname in allowed and target.port in (None, 80) and r.method == 'GET':
                    # Some old official login buttons still point at HTTP. Upgrade
                    # locally before any cookie or CAS ticket crosses the network.
                    return route.fulfill(status=307, headers={'location': 'https://' + target.hostname + target._replace(scheme='', netloc='').geturl()})
                if r.is_navigation_request() and (urlsplit(r.url).scheme != 'https' or urlsplit(r.url).hostname not in allowed):
                    return route.abort()
                return route.continue_()
            context.route('**/*', guard)
            session = Session(site, context, account)
            try:
                yield session
                if session.verified:
                    state = scoped(site, context.storage_state(indexed_db=True))
                    if native.enabled():
                        native.invoke('sessions_write', site, ServiceError, path=session_path(site), account=account,
                                      value={'account': account, 'saved_at': time.time(), 'state': state})
                    else:
                        write_private(session_path(site), {'account': account, 'saved_at': time.time(), 'state': state})
                    save_device_state(account, state, 'chrome', {})
                    limiter.success(site)
            finally:
                context.close()
    except PolicyError as exc:
        raise policy_error(ServiceError, exc) from None
    except BrowserTimeout:
        limiter.failure(site)
        raise ServiceError('平台加载超时，尚未确认结果；请稍后恢复查询。', code='timeout') from None
