import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from playwright.sync_api import Error as PlaywrightError, sync_playwright
from ..bb.email_verification import EmailVerification
from ..bb.identity import IDENTITY_HOSTS, load_credentials, load_device_state, save_device_state
from ..bb.login import CONTEXT_OPTIONS, remember_device, try_submit_credentials
from ..bb.session import BBError
from ..browser import BackgroundVerificationRequired, background_progress, chrome_browser, stop_if_manual_verification
from ..mail.config import local_dir
from .client import authenticated_portal, install_guard, smart_entry
from .session import HOME, HOST, FinanceError, load_session, save_session


def login_state(stage, detail, **metadata):
    detail, policy = background_progress(stage, detail)
    directory = local_dir()
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / 'finance.login-status.tmp'
    temporary.write_text(json.dumps({'stage': stage, 'detail': detail, 'updated_at': datetime.now(timezone.utc).isoformat(),
                                     **metadata, **policy}, ensure_ascii=False), encoding='utf-8')
    temporary.replace(directory / 'finance.login-status.json')
    stop_if_manual_verification(stage)


def run(timeout_seconds=240):
    login_state('starting', '正在后台连接财务综合信息平台。')
    try:
        credentials = load_credentials()
        try:
            device = load_device_state(credentials['username'])
        except BBError:
            device = None
        state = device['state'] if device else {'cookies': [], 'origins': []}
        try:
            previous = load_session()['state']
            state = {key: state[key] + previous[key] for key in ('cookies', 'origins')}
        except FinanceError:
            pass
        with sync_playwright() as p, chrome_browser(p) as browser:
            options = dict(CONTEXT_OPTIONS)
            if device:
                options.update({k:v for k,v in device.get('context_options', {}).items() if k in {*CONTEXT_OPTIONS, 'user_agent'}})
            options.setdefault('user_agent', f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{browser.version} Safari/537.36')
            context = browser.new_context(**options, storage_state=state, service_workers='block')
            pending, _ = install_guard(context, login=True)
            page = context.new_page()
            try:
                page.goto(HOME, wait_until='domcontentloaded', timeout=35000)
            except PlaywrightError:
                if not pending:
                    raise
            submitted = False
            verification = EmailVerification(credentials.get('email_verification_enabled') is True, login_state,
                remember_device, expected_address=credentials.get('email_verification_address'))
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if pending:
                    target = pending.pop(0)
                    page.goto(target, wait_until='domcontentloaded', timeout=30000)
                if urlsplit(page.url).hostname in IDENTITY_HOSTS:
                    remember_device(page)
                    if not submitted:
                        submitted = try_submit_credentials(page, credentials)
                    if submitted:
                        verification.step(page)
                        text = page.locator('body').inner_text(timeout=3000)
                        verification.observe_result(text)
                        if re.search(r'密码错误|用户名或密码不正确|Invalid credentials', text, re.I):
                            raise FinanceError('学校未接受已保存凭据，已停止自动重试。')
                        if not verification.last_stage and re.search(r'验证码|动态口令|二次验证|Verification code', text, re.I):
                            login_state('waiting_for_verification', '学校要求人工身份验证。')
                if authenticated_portal(page):
                    smart_entry(page.content())  # Validate actual portal entry, do not guess a ticket.
                    state = context.storage_state(indexed_db=True)
                    save_session(state, credentials['username'], options['user_agent'])
                    save_device_state(credentials['username'], state, 'chrome', options)
                    login_state('connected', '财务门户登录已验证，会话已加密保存；智能报销页面可单独检查。',
                                connected=True, scope='financial_portal', credential_submission_count=int(submitted),
                                email_code_requested=verification.requested)
                    return
                page.wait_for_timeout(400)
            raise FinanceError('财务后台登录等待超时，已有会话保留。')
    except BackgroundVerificationRequired:
        return
    except (BBError, FinanceError) as exc:
        login_state('error', str(exc))
        raise FinanceError(str(exc)) from None
    except PlaywrightError:
        login_state('error', '财务后台 Chrome 登录未完成，请检查本地进度。')
        raise FinanceError('财务后台登录未完成；不会自动打开桌面窗口。') from None
