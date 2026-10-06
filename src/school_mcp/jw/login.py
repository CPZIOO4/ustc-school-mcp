from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from ..browser import BackgroundVerificationRequired, background_progress, chrome_browser, stop_if_manual_verification

from ..bb.email_verification import EmailVerification
from ..bb.identity import IDENTITY_HOSTS, load_credentials, load_device_state, save_device_state
from ..bb.login import CONTEXT_OPTIONS, remember_device, try_submit_credentials
from ..bb.session import BBError
from ..mail.config import local_dir
from .client import JWClient
from .session import BASE_URL, HOST, JWError, jw_cookies, load_session, save_session


def login_state(stage: str, detail: str, **metadata):
    detail, policy = background_progress(stage, detail)
    metadata.update(policy)
    directory = local_dir()
    directory.mkdir(parents=True, exist_ok=True)
    data = {"stage": stage, "detail": detail, "updated_at": datetime.now(timezone.utc).isoformat(), **metadata}
    temporary = directory / "jw.login-status.tmp"
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(directory / "jw.login-status.json")
    stop_if_manual_verification(stage)


def run(timeout_seconds=240):
    try:
        credentials = load_credentials()
    except BBError:
        credentials = None
    account = credentials["username"] if credentials else None
    try:
        device = load_device_state(account)
    except BBError:
        device = None
    login_state("starting", "正在恢复设备信任状态并打开教务系统。")
    try:
        with sync_playwright() as p, chrome_browser(p) as browser:
            channel = "chrome"
            options = dict(CONTEXT_OPTIONS)
            if device:
                options.update({k: v for k, v in device.get("context_options", {}).items() if k in {*CONTEXT_OPTIONS, "user_agent"}})
            options.setdefault("user_agent", f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{browser.version} Safari/537.36")
            context = browser.new_context(**options, storage_state=device["state"] if device else None)
            try:
                context.add_cookies(load_session()["cookies"])
            except JWError:
                pass
            page = context.new_page()
            # This is the SSO link exposed by the academic system's real login page.
            page.goto(BASE_URL + "/ucas-sso/login", wait_until="domcontentloaded", timeout=45000)
            login_state("authenticating", "正在使用已保存的学校统一身份状态登录教务系统。", device_state_restored=device is not None)
            verification = EmailVerification(bool(credentials and credentials.get("email_verification_enabled") is True), login_state, remember_device, expected_address=credentials.get("email_verification_address") if credentials else None)
            deadline, next_check, submitted = time.monotonic() + timeout_seconds, 0.0, False
            while browser.is_connected() and time.monotonic() < deadline and context.pages:
                page = context.pages[-1]
                hostname = urlsplit(page.url).hostname
                if hostname in IDENTITY_HOSTS:
                    remember_device(page)
                    if not submitted:
                        submitted = try_submit_credentials(page, credentials)
                        if submitted:
                            login_state("credentials_submitted", "已自动提交统一身份凭据，正在等待教务登录结果。")
                    if submitted:
                        verification.step(page)
                        text = page.locator("body").inner_text(timeout=3000)
                        verification.observe_result(text)
                        if re.search(r"密码错误|用户名或密码不正确|Invalid credentials", text, re.I):
                            raise JWError("学校未接受已保存的统一身份凭据，已停止自动重试。")
                        if not verification.last_stage and re.search(r"验证码|动态口令|二次验证|Verification code", text, re.I):
                            login_state("waiting_for_verification", "学校要求身份验证，请在教务登录窗口完成。")
                if hostname == HOST and time.monotonic() >= next_check:
                    next_check = time.monotonic() + 3
                    cookies = jw_cookies(context.cookies())
                    if cookies:
                        try:
                            result = JWClient(cookies=cookies, user_agent=options["user_agent"]).check()
                        except JWError:
                            pass
                        else:
                            save_session(cookies, options["user_agent"])
                            save_device_state(account, context.storage_state(indexed_db=True), channel, options)
                            metadata = {"device_state_restored": device is not None, "credential_submission_count": int(submitted), "email_code_requested": verification.requested}
                            login_state("connected", "教务系统会话已加密保存，可以继续查询。", **metadata)
                            print(json.dumps({**result, **metadata}, ensure_ascii=False))
                            browser.close()
                            return
                page.wait_for_timeout(350)
            login_state("cancelled", "后台登录已停止或等待超时；已有认证状态仍保留。")
            browser.close()
    except BackgroundVerificationRequired:
        return
    except (JWError, BBError) as exc:
        login_state("error", str(exc))
        raise JWError(str(exc)) from None
    except PlaywrightError:
        login_state("error", "教务系统后台登录未能完成认证；已有认证状态仍保留。")
        raise JWError("教务系统登录未完成，请检查本地登录进度。") from None
