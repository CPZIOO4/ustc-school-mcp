"""USTC login with user-authorized credentials and encrypted remembered-device state."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from ..browser import BackgroundVerificationRequired, background_progress, chrome_browser, stop_if_manual_verification

from school_mcp.mail.config import local_dir

from .client import BBClient
from .email_verification import EmailVerification
from .identity import AUTH_HOSTS, IDENTITY_HOSTS, load_credentials, load_device_state, save_device_state
from .session import BASE_URL, PORTAL_PATH, BBError, bb_cookies, load_session, save_session

CONTEXT_OPTIONS = {"locale": "zh-CN", "timezone_id": "Asia/Shanghai", "viewport": {"width": 1280, "height": 900}, "color_scheme": "light"}


def login_state(stage: str, detail: str, **metadata) -> None:
    detail, policy = background_progress(stage, detail)
    metadata.update(policy)
    directory = local_dir()
    directory.mkdir(parents=True, exist_ok=True)
    data = {"stage": stage, "detail": detail, "updated_at": datetime.now(timezone.utc).isoformat(), **metadata}
    temporary = directory / "bb.login-status.tmp"
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(directory / "bb.login-status.json")
    stop_if_manual_verification(stage)


def sso_url_from_html(page_url: str, html: str) -> str | None:
    """Use links from the authenticated BB page after its network gateway."""
    try:
        source = urlsplit(page_url)
        if (source.scheme != "https" or source.hostname not in {"www.bb.ustc.edu.cn", "bb.ustc.edu.cn"}
                or source.port not in (None, 443) or source.username or source.password):
            return None
        for anchor in BeautifulSoup(html, "html.parser").select("a[href]"):
            candidate = urljoin(page_url, anchor["href"])
            parsed = urlsplit(candidate)
            if (parsed.scheme == "https" and parsed.hostname in IDENTITY_HOSTS
                    and parsed.port in (None, 443) and not parsed.username and not parsed.password
                    and ("统一身份" in anchor.get_text() or "login" in parsed.path)):
                return candidate
    except ValueError:
        pass
    return None


def find_sso_url() -> str | None:
    # Fresh unauthenticated BB HTML supplies its registered CAS service URL.
    try:
        with httpx.Client(follow_redirects=True, timeout=15) as client:
            response = client.get(BASE_URL + "/webapps/login/")
        return sso_url_from_html(str(response.url), response.text)
    except httpx.HTTPError:
        pass
    return None


def gateway_login_url(page_url: str, html: str) -> str | None:
    """Recognize the school's off-campus gateway without widening credential hosts."""
    try:
        source = urlsplit(page_url)
        if (source.scheme != "https" or source.hostname not in {"www.bb.ustc.edu.cn", "bb.ustc.edu.cn"}
                or source.port not in (None, 443) or source.username or source.password
                or source.path != "/nginx_auth/"):
            return None
        candidates = set()
        for anchor in BeautifulSoup(html, "html.parser").select("a[href]"):
            if anchor.get_text(strip=True) != "登录":
                continue
            candidate = urljoin(page_url, anchor["href"])
            target = urlsplit(candidate)
            if (target.scheme == "https" and target.hostname == source.hostname
                    and target.port in (None, 443) and not target.username and not target.password
                    and target.path == "/nginx_auth/login.php" and not target.fragment):
                candidates.add(candidate)
        return next(iter(candidates)) if len(candidates) == 1 else None
    except ValueError:
        return None


def try_submit_credentials(page, credentials: dict | None) -> bool:
    parsed = urlsplit(page.url)
    if credentials is None or parsed.scheme != "https" or parsed.hostname not in IDENTITY_HOSTS or parsed.port not in (None, 443) or parsed.username or parsed.password:
        return False
    account = page.locator('input[placeholder*="学工号"]:visible, input[placeholder*="GID"]:visible, input[name="username"]:visible, input#username:visible')
    password = page.locator('input[type="password"]:visible')
    if account.count() != 1 or password.count() != 1:
        return False
    button = page.get_by_role("button", name=re.compile(r"^(Sign In|Log In|立即登录|登录|登\s*录)$", re.I))
    if button.count() != 1:
        button = page.get_by_text("Sign In", exact=True)
    if button.count() != 1:
        button = page.locator('input[type="submit"]:visible')
    if button.count() != 1:
        return False
    account.fill(credentials["username"])
    password.fill(credentials["password"])
    remember_device(page)
    button.click()
    return True


def remember_device(page) -> None:
    parsed = urlsplit(page.url)
    if parsed.scheme != "https" or parsed.hostname not in IDENTITY_HOSTS:
        return
    checkbox = page.get_by_role("checkbox", name=re.compile(r"信任.*设备|记住.*设备|Trust.*device|Remember.*device", re.I))
    if checkbox.count() == 1 and checkbox.is_visible() and not checkbox.is_checked():
        checkbox.check()


def run(timeout_seconds: int = 240, force_identity_login: bool = False) -> None:
    credentials = None
    try:
        credentials = load_credentials()
    except BBError:
        pass
    account = credentials["username"] if credentials else None
    device = None
    try:
        device = load_device_state(account)
    except BBError:
        pass
    login_state("starting", "正在恢复统一身份设备状态并在后台登录 BB。", credentials_saved=credentials is not None, device_state_restored=device is not None)
    try:
        with sync_playwright() as playwright, chrome_browser(playwright) as browser:
            channel = "chrome"
            options = dict(CONTEXT_OPTIONS)
            if device:
                options.update({key: value for key, value in device.get("context_options", {}).items() if key in {*CONTEXT_OPTIONS, "user_agent"}})
            if "user_agent" not in options:
                options["user_agent"] = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{browser.version} Safari/537.36"
            context = browser.new_context(**options, storage_state=device["state"] if device else None)
            try:
                context.add_cookies(load_session()["cookies"])
            except BBError:
                pass
            page = context.new_page()
            url = find_sso_url() if force_identity_login else None
            page.goto(url or BASE_URL + PORTAL_PATH, wait_until="domcontentloaded", timeout=45000)
            login_state("authenticating", "正在使用已保存的会话或统一身份凭据登录。", credentials_saved=credentials is not None, device_state_restored=device is not None)
            deadline = time.monotonic() + timeout_seconds
            next_check = 0.0
            submitted = False
            email_verification = EmailVerification(bool(credentials and credentials.get("email_verification_enabled") is True), login_state, remember_device, expected_address=credentials.get("email_verification_address") if credentials else None)
            followed_sso = url is not None
            followed_gateway = False
            identity_visited = url is not None
            last_location = None
            while time.monotonic() < deadline and browser.is_connected():
                if not context.pages:
                    break
                page = context.pages[-1]
                hostname = urlsplit(page.url).hostname
                location = (hostname, urlsplit(page.url).path)
                if location != last_location and hostname in AUTH_HOSTS:
                    last_location = location
                    login_state("authenticating", "正在核对学校认证跳转。", page_host=hostname, page_path=location[1])
                if not followed_gateway and urlsplit(page.url).path == "/nginx_auth/":
                    gateway = gateway_login_url(page.url, page.content())
                    if gateway:
                        followed_gateway = True
                        login_state("authenticating", "正在通过学校校外访问入口恢复认证。")
                        page.goto(gateway, wait_until="domcontentloaded", timeout=45000)
                        continue
                identity_visited = identity_visited or hostname in IDENTITY_HOSTS
                remember_device(page)
                if not submitted:
                    submitted = try_submit_credentials(page, credentials)
                    if submitted:
                        login_state("credentials_submitted", "已自动提交统一身份登录信息。已启用的邮箱验证会自动尝试。", device_state_restored=device is not None)
                if hostname in IDENTITY_HOSTS:
                    if submitted:
                        email_verification.step(page)
                    text = page.locator("body").inner_text(timeout=3000)
                    email_verification.observe_result(text)
                    if submitted and not email_verification.last_stage and re.search(r"短信.*验证|请输入.*验证码|动态口令|二次验证|Verification code", text, re.I):
                        login_state("waiting_for_verification", "学校要求身份验证，请在登录窗口输入验证码，并勾选信任此设备。完成后会保存设备 Cookie。", device_state_restored=device is not None)
                    if submitted and re.search(r"密码错误|用户名或密码不正确|Invalid credentials", text, re.I):
                        raise BBError("学校未接受已保存的统一身份凭据，已停止自动重试。")
                if time.monotonic() >= next_check:
                    next_check = time.monotonic() + 3
                    if hostname in {"www.bb.ustc.edu.cn", "bb.ustc.edu.cn"} and urlsplit(page.url).path.startswith("/webapps/login") and not followed_sso:
                        sso = sso_url_from_html(page.url, page.content()) or find_sso_url()
                        if sso:
                            page.goto(sso, wait_until="domcontentloaded", timeout=45000)
                            followed_sso = True
                            identity_visited = True
                            continue
                    cookies = bb_cookies(context.cookies())
                    # A force-auth run must reach BB after the identity flow; old BB cookies alone are insufficient.
                    may_verify = not force_identity_login or (identity_visited and hostname in {"www.bb.ustc.edu.cn", "bb.ustc.edu.cn"})
                    if may_verify and any("session" in cookie.get("name", "").lower() for cookie in cookies):
                        try:
                            result = BBClient(cookies=cookies).check()
                        except BBError:
                            if not followed_sso:
                                sso = sso_url_from_html(page.url, page.content()) or find_sso_url()
                                if sso:
                                    page.goto(sso, wait_until="domcontentloaded", timeout=45000)
                                    followed_sso = True
                                    identity_visited = True
                        else:
                            save_session(cookies)
                            saved = save_device_state(account, context.storage_state(indexed_db=True), channel, options)
                            identity_cookies = [c for c in saved["state"]["cookies"] if c["domain"].lstrip(".") in IDENTITY_HOSTS | {"ustc.edu.cn"}]
                            metadata = {"device_state_restored": device is not None, "credential_submission_count": int(submitted), "identity_cookie_count": len(identity_cookies), "identity_persistent_cookie_count": sum(c.get("expires", -1) > time.time() for c in identity_cookies), "email_verification_enabled": email_verification.enabled, "email_code_requested": email_verification.requested, "email_code_submitted": email_verification.submitted}
                            login_state("connected", "BB 会话与统一身份设备状态已加密保存。", **metadata)
                            print(json.dumps({**result, **metadata}, ensure_ascii=False))
                            browser.close()
                            return
                # Pump Playwright events so a user-driven navigation or closed window is observed promptly.
                page.wait_for_timeout(350)
            login_state("cancelled", "后台登录已停止或等待超时；之前保存的设备状态仍保留。")
            browser.close()
    except BackgroundVerificationRequired:
        return
    except BBError as exc:
        login_state("error", str(exc))
        raise
    except PlaywrightError:
        login_state("error", "BB 后台登录未能完成认证；已有会话和信任设备状态仍保留。")
        raise BBError("BB 后台登录未能完成认证，请检查本地登录状态。") from None
