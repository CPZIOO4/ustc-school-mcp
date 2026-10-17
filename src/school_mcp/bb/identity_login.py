"""Fixed CAS authentication workflow, shared by the school adapters.

Secrets are loaded locally. No URL, JavaScript, password or OTP is a tool input.
An authenticated page and a fresh-context reuse check are required for success.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from ..browser import BackgroundVerificationRequired, background_progress, chrome_browser, stop_if_manual_verification
from ..login_runtime import progress, start_login
from ..mail.config import local_dir
from .email_verification import EmailVerification
from .identity import load_credentials, load_device_state, save_device_state, status
from .login import CONTEXT_OPTIONS, remember_device, try_submit_credentials
from .session import BBError

ENTRY = 'https://id.ustc.edu.cn/cas/login'


def authenticated(page) -> bool:
    parsed = urlsplit(page.url)
    if (parsed.scheme != 'https' or parsed.hostname != 'id.ustc.edu.cn'
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.path.rstrip('/') != '/gate/cas-success'):
        return False
    if page.locator('input[type="password"]:visible').count():
        return False
    text = page.locator('body').inner_text(timeout=3000)
    return all(label in text for label in ('个人信息', '安全设置', '访问历史'))


def login_state(stage: str, detail: str, **metadata) -> None:
    detail, policy = background_progress(stage, detail)
    directory = local_dir()
    directory.mkdir(parents=True, exist_ok=True)
    data = dict(stage=stage, detail=detail, updated_at=datetime.now(timezone.utc).isoformat(), **metadata, **policy)
    temporary = directory / 'identity.login-status.tmp'
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    temporary.replace(directory / 'identity.login-status.json')
    stop_if_manual_verification(stage)


def auth_status() -> dict:
    return {**status(), **progress('identity')}


def start() -> dict:
    return start_login('identity', login_state, BBError)


def wait_authenticated(page, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not page.is_closed():
        if authenticated(page):
            return True
        page.wait_for_timeout(350)
    return False


def verify_saved(browser, account: str, options: dict) -> bool:
    """Read the encrypted file back; memory-only state is insufficient evidence."""
    saved = load_device_state(account)
    context = browser.new_context(**options, storage_state=saved['state'])
    try:
        page = context.new_page()
        page.goto(ENTRY, wait_until='domcontentloaded', timeout=45000)
        return wait_authenticated(page)
    finally:
        context.close()


def check_connection() -> dict:
    """Only probe saved state: never submit credentials or request a message."""
    credentials = load_credentials()
    with sync_playwright() as p, chrome_browser(p, headless=True) as browser:
        options = context_options(browser, load_device_state(credentials['username']))
        try:
            connected = verify_saved(browser, credentials['username'], options)
        except PlaywrightError:
            raise BBError('统一身份状态核验未完成，请检查网络后重试；已有状态保留。') from None
    return dict(connected=connected, network_checked=True, saved_state_reused=connected,
                credential_submission_count=0, email_code_requested=False,
                scope='ustc_identity', next_step='继续目标网站的连接检查。' if connected else '调用 school_bb_identity_reconnect。')


def context_options(browser, device) -> dict:
    options = dict(CONTEXT_OPTIONS)
    if device:
        options.update({k: v for k, v in device.get('context_options', {}).items() if k in {*CONTEXT_OPTIONS, 'user_agent'}})
    options.setdefault('user_agent', f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{browser.version} Safari/537.36')
    return options


def run(timeout_seconds: int = 240) -> dict | None:
    try:
        credentials = load_credentials()
        account = credentials['username']
        try:
            device = load_device_state(account)
        except BBError:
            device = None
        login_state('starting', '正在恢复统一身份登录态并打开学校官方认证入口。')
        with sync_playwright() as p, chrome_browser(p) as browser:
            options = context_options(browser, device)
            context = browser.new_context(**options, storage_state=device['state'] if device else None)
            try:
                page = context.new_page()
                page.goto(ENTRY, wait_until='domcontentloaded', timeout=45000)
                verification = EmailVerification(credentials.get('email_verification_enabled') is True,
                    login_state, remember_device, expected_address=credentials.get('email_verification_address'))
                deadline, submitted = time.monotonic() + timeout_seconds, False
                login_state('authenticating', '正在检查统一身份状态，必要时进行已授权的邮箱验证。')
                while time.monotonic() < deadline and browser.is_connected() and not page.is_closed():
                    if authenticated(page):
                        save_device_state(account, context.storage_state(indexed_db=True), 'chrome', options)
                        login_state('verifying_saved_state', '登录态已加密保存，正在用独立浏览器会话核验复用。')
                        if not verify_saved(browser, account, options):
                            login_state('waiting_for_verification', '学校未允许独立会话复用登录态；不会重复提交密码或发送验证码。')
                            return None
                        result = dict(connected=True, network_checked=True, scope='ustc_identity',
                            saved_state_reused=True, device_state_restored=device is not None,
                            credential_submission_count=int(submitted), email_code_requested=verification.requested,
                            email_code_submitted=verification.submitted)
                        login_state('connected', '统一身份登录态与设备信任状态已加密保存，并通过独立会话复用核验。', **result)
                        return result
                    parsed = urlsplit(page.url)
                    if parsed.scheme != 'https' or parsed.hostname != 'id.ustc.edu.cn' or parsed.port not in (None, 443) or parsed.username or parsed.password:
                        raise BBError('统一身份页面离开允许的官方来源，流程已停止。')
                    remember_device(page)
                    if not submitted:
                        submitted = try_submit_credentials(page, credentials)
                    if submitted or credentials.get('session_only') is True or device is not None:
                        verification.step(page)
                    text = page.locator('body').inner_text(timeout=3000)
                    verification.observe_result(text)
                    if submitted and any(value in text for value in ('密码错误', '用户名或密码不正确', 'Invalid credentials')):
                        raise BBError('学校未接受已保存的统一身份凭据；已停止自动重试。')
                    if not credentials.get('password') and page.locator('input[type="password"]:visible').count():
                        login_state('waiting_for_verification', '保存的会话已失效，需要本人重新登录。')
                    if verification.needs_manual(text):
                        login_state('waiting_for_verification', '学校未提供可自动完成的邮箱验证，需要本人完成当前验证。')
                    page.wait_for_timeout(350)
                login_state('cancelled', '统一身份登录超时或窗口关闭；不会自动重试，已有状态保留。')
            finally:
                context.close()
    except BackgroundVerificationRequired:
        return None
    except BBError:
        login_state('error', '统一身份登录未完成，请检查本机凭据、邮箱授权和连接；已有状态保留。')
        raise
    except PlaywrightError:
        login_state('error', '统一身份后台浏览器或网络异常；已有状态保留，不会重发验证码。')
        raise BBError('统一身份登录未完成，请查看本地登录进度。') from None


def main() -> None:
    result = run()
    if result:
        print(json.dumps(result, ensure_ascii=False))
