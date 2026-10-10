from __future__ import annotations

from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
from bs4 import BeautifulSoup
import httpx
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright
from .. import network
from ..service_errors import identity_redirect, policy_error
from ..bb.identity import IDENTITY_HOSTS
from ..bb.login import CONTEXT_OPTIONS
from ..browser import chrome_browser
from .session import FinanceError, HOST, HOME, ORIGIN, SMART_HOME, PUBLIC_HOME, GUIDE, load_session
from .catalog import CatalogBootstrap, HOME_READY, CATALOG_QUERY, catalog_result

INIT_PATHS = {'/YBX/getWinTypes.action', '/YBX/loadRolesMenu.action', '/YBX/loadInitFunction.action',
              '/YBX/loadRolesName.action', '/YBX/getDefaultWinno.action'}
STATIC_SUFFIXES = ('.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.woff', '.woff2', '.ttf', '.eot')
STATIC_RESOURCES = {'/YBX/devel/source/src/resources/editor.txt', '/YBX/devel/source/src/resources/graph.txt',
                    '/YBX/devel/source/src/resources/editor_zh.txt', '/YBX/devel/source/src/resources/graph_zh.txt',
                    '/YBX/scjs/S_2021_0045/customjs/scpe_ybx.json'}
MODULES = ('日常报销', '国内差旅', '借款业务', '薪酬发放', '校内转账', '全部报销单',
           '草稿', '审批中', '已审批', '待支付', '退回', '附件补录', '待我审批', '我已审批', '发票')


def portal_entries(html: str) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    result = []
    for identifier, label in [('YBX', '智能报销'), ('WF_YB4_6', '旧网上报账'), ('WF_CWQ', '财务查询'),
                              ('WF_CWBS', '财务报表'), ('JS_JSON', '结算相关入口')]:
        node = soup.find(id=identifier)
        if node and node.get('url'):
            result.append({'service_id': identifier, 'name': label, 'available_in_portal': True,
                           'business_operations_verified': False})
    return result


def smart_entry(html: str) -> str:
    node = BeautifulSoup(html, 'html.parser').find(id='YBX')
    if not node or not node.get('url'):
        raise FinanceError('财务门户未提供智能报销入口，不能猜测跳转参数。')
    target = urljoin(HOME, node['url'].removeprefix('WINOPEN').removeprefix('ERPURL'))
    parsed = urlsplit(target)
    if (parsed.hostname != HOST or parsed.scheme != 'https' or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.path != '/YBX/main2.jsp'):
        raise FinanceError('智能报销入口发生变化，请重新核对官方跳转。')
    return target  # Ephemeral SSO arguments stay inside the connector.


def request_allowed(url, method, body='', *, login=False):
    try:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.username or parsed.password:
            return False
    except ValueError:
        return False
    if login and parsed.hostname in IDENTITY_HOSTS:
        return True
    if parsed.hostname != HOST:
        return False
    path = parsed.path.split(';')[0]
    if method in {'GET', 'HEAD'}:
        return path in {'/WFManager/home2.jsp', '/YBX/main2.jsp', '/YBX/main.jsp'} | STATIC_RESOURCES or path.endswith(STATIC_SUFFIXES)
    if method != 'POST':
        return False
    if path in INIT_PATHS and not parsed.query:
        # loadRolesMenu has the framework's encrypted transport envelope. No
        # arbitrary caller-supplied POST API is exposed by this connector.
        return path == '/YBX/loadRolesMenu.action' or not body
    return (path == '/YBX/redirectCustomPage.action' and not body
            and parse_qs(parsed.query) == {'url': ['Customize/CustomJsList.json']})


def install_guard(context, *, login=False, catalog=None):
    pending, blocked = [], set()
    def guard(route):
        request = route.request
        parsed = urlsplit(request.url)
        path = parsed.path.split(';')[0]
        if (login and parsed.scheme == 'http' and parsed.hostname == HOST and parsed.port in (None, 80)
                and not parsed.username and not parsed.password and request.method == 'GET'
                and request.is_navigation_request() and path in {'/WFManager/home2.jsp', '/YBX/main2.jsp', '/YBX/main.jsp'}):
            # The published CAS service uses HTTP. Abort before sending the
            # ticket, then navigate its observed callback over HTTPS instead.
            pending.append(urlunsplit(parsed._replace(scheme='https', netloc=HOST)))
            route.abort()
            return
        allowed = request_allowed(request.url, request.method, request.post_data or '', login=login)
        if not allowed and catalog is not None:
            allowed = catalog.allow(request.url, request.method, request.post_data or '')
        if not allowed:
            blocked.add((request.method, parsed.hostname or '', path))
            route.abort()
            return
        if parsed.hostname == HOST and request.method == 'POST':
            try:
                network.limiter.acquire('finance')
            except network.PolicyError as exc:
                if catalog is not None:
                    catalog.policy_error = exc
                route.abort()
                return
        if (catalog is not None and parsed.hostname == HOST and request.method == 'POST'
                and path in {'/YBX/getDefaultWinno.action', '/YBX/common_updateUserContext.action', '/YBX/loadDefinition.action'}):
            # Observe the response before delivering it to the page. A response
            # event can arrive after JS already dispatched the dependent POST.
            try:
                response = route.fetch(max_redirects=0, timeout=15000)
                network.limiter.response('finance', response.status, response.headers)
                body = response.text() if path != '/YBX/loadDefinition.action' else ''
                catalog.observe(path, response.status, body)
                route.fulfill(response=response)
            except network.PolicyError as exc:
                catalog.policy_error = exc
                route.abort()
            except PlaywrightError:
                route.abort()
            return
        route.continue_()
    context.route('**/*', guard)
    return pending, blocked


def authenticated_portal(page):
    parsed = urlsplit(page.url)
    return (parsed.scheme == 'https' and parsed.hostname == HOST
            and parsed.path.split(';')[0] == '/WFManager/home2.jsp'
            and bool(portal_entries(page.content())))


class FinanceClient:
    def __init__(self, session=None, transport=None):
        self.session = session
        self.transport = transport

    def _session(self):
        return self.session if self.session is not None else load_session()

    def services(self):
        session = self._session()
        jar = httpx.Cookies()
        for cookie in session['state']['cookies']:
            if cookie.get('domain', '').lstrip('.') == HOST:
                jar.set(cookie['name'], cookie['value'], domain=cookie['domain'], path=cookie.get('path', '/'))
        try:
            with network.http_client('finance', FinanceError, cookies=jar, timeout=20,
                                     follow_redirects=False, transport=self.transport) as client:
                response = client.get(HOME)
                if response.status_code in {401, 403}:
                    raise FinanceError('财务会话失效或访问受限。', code='authentication_required' if response.status_code == 401 else 'access_denied')
                if response.is_redirect:
                    target = urljoin(str(response.url), response.headers.get('location', ''))
                    raise FinanceError('财务门户发生跳转。', code='authentication_required' if identity_redirect(target) else 'unavailable')
                response.raise_for_status()
                if len(response.content) > 5 * 1024 * 1024:
                    raise FinanceError('财务门户页面超过读取限制。')
                entries = portal_entries(response.text)
                # The authenticated portal embeds a hidden change-password dialog.
                # A password input alone is not evidence that authentication failed.
                if not entries and BeautifulSoup(response.text, 'html.parser').select_one('input[type="password"]'):
                    raise FinanceError('财务门户需要认证。', code='authentication_required')
                if not entries:
                    raise FinanceError('未确认财务门户业务入口，不能将登录页或空页面当作成功。')
                return {'connected': True, 'source': HOME, 'services': entries, 'read_only': True,
                        'scope': 'financial_portal', 'content_is_untrusted': True}
        except httpx.HTTPError:
            raise FinanceError('财务门户读取未完成，请检查网络或会话。') from None

    def check(self):
        result = self.services()
        return {'connected': result['connected'], 'scope': result['scope'],
                'smart_entry_available': any(s['service_id'] == 'YBX' for s in result['services']),
                'business_operations_verified': False, 'read_only': True}

    def inspect_smart(self):
        session = self._session()
        options = dict(CONTEXT_OPTIONS)
        if session.get('user_agent'):
            options['user_agent'] = session['user_agent']
        try:
            network.limiter.acquire('finance')
            with sync_playwright() as p, chrome_browser(p, headless=True) as browser:
                context = browser.new_context(**options, storage_state=session['state'], service_workers='block')
                bootstrap = CatalogBootstrap()
                _, blocked = install_guard(context, catalog=bootstrap)
                page = context.new_page()
                def observe(response):
                    if urlsplit(response.url).hostname != HOST or response.request.method != 'POST':
                        return
                    try:
                        network.limiter.response('finance', response.status, response.headers)
                    except network.PolicyError as exc:
                        bootstrap.policy_error = exc
                page.on('response', observe)
                response = page.goto(HOME, wait_until='domcontentloaded', timeout=30000)
                if response is not None:
                    network.limiter.response('finance', response.status, response.headers)
                    if response.status in {401, 403}:
                        raise FinanceError('财务门户认证失败。', code='authentication_required' if response.status == 401 else 'access_denied')
                if identity_redirect(page.url):
                    raise FinanceError('财务门户需要认证。', code='authentication_required')
                if not authenticated_portal(page):
                    raise FinanceError('财务门户会话失效，请调用 school_finance_reconnect。', code='authentication_required')
                target = smart_entry(page.content())
                network.limiter.acquire('finance')
                response = page.goto(target, wait_until='domcontentloaded', timeout=30000)
                if response is not None:
                    network.limiter.response('finance', response.status, response.headers)
                page.get_by_text('欢迎您', exact=False).first.wait_for(timeout=20000)
                location = urlsplit(page.url)
                if location.hostname != HOST or location.path.split(';')[0] != '/YBX/main.jsp':
                    raise FinanceError('未到达预期的智能报销系统。')
                catalog = {'catalog_complete': False, 'catalog_state': 'homepage_not_ready', 'business_entries': []}
                try:
                    page.wait_for_function(HOME_READY, timeout=15000)
                    if bootstrap.definition_ready and bootstrap.default_window is not None:
                        bootstrap.query_armed = True
                        try:
                            catalog = catalog_result(page.evaluate(CATALOG_QUERY, bootstrap.default_window))
                        finally:
                            bootstrap.query_armed = False
                except PlaywrightTimeoutError:
                    pass
                if bootstrap.policy_error is not None:
                    raise policy_error(FinanceError, bootstrap.policy_error)
                # The hidden framework menu contains admin/configuration labels.
                # It must never serve as evidence of account business capabilities.
                visible = page.locator('#main .searchBarBody .title_content:visible').all_text_contents()
                visible = [label for label in visible if label in MODULES]
                return {'connected': True,
                        'scope': 'authenticated_smart_catalog' if catalog['catalog_complete'] else 'authenticated_smart_shell',
                        'source': SMART_HOME, 'visible_module_labels': visible, **catalog,
                        'page_initialization_complete': False, 'session_context_initialized': bootstrap.context_ready,
                        'business_operations_verified': False, 'read_only': True, 'headless': True,
                        'content_is_untrusted': True,
                        'blocked_requests': [{'method': m, 'host': h, 'path': path} for m,h,path in sorted(blocked)],
                        'next_action': ('按 business_entries 选择目录；有 guide_business_type 时调用 school_finance_workflow_guide 准备材料。实际表单尚未适配。'
                                        if catalog['catalog_complete'] else
                                        '目录尚未完整读取，请根据 catalog_state 处理；不可据此判断没有业务或自动执行报销。'),
                        'note': '目录来自当前账号首页的业务查询，不是隐藏配置树；完整网页初始化中的不透明过程仍被拦截。目录完整不表示网页完整或已获办理权限；不返回个人金额、票据、单据或跳转参数。'}
        except network.PolicyError as exc:
            raise policy_error(FinanceError, exc) from None
        except PlaywrightError:
            network.limiter.failure('finance')
            raise FinanceError('智能报销后台页面未完成读取，可能会话失效或初始化尚未适配。') from None


def entry_points():
    return {'official_site': PUBLIC_HOME, 'portal': HOME, 'guide_notice': GUIDE,
            'route': ['财务处官网', '财务综合信息平台', '学校统一身份认证', '智能报销'],
            'source_verified_on': '2026-10-08', 'network_checked': False,
            'note': '固定入口来自官网及2025年4月操作指南；本工具不验证当前登录。'}
