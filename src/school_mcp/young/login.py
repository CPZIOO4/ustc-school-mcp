from __future__ import annotations

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
from .client import authenticated_page
from .session import HOME, HOST, YoungError, load_session, save_session


def login_state(stage: str, detail: str, **metadata):
    detail, policy = background_progress(stage, detail)
    directory = local_dir()
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "young.login-status.tmp"
    temporary.write_text(json.dumps({"stage": stage, "detail": detail, "updated_at": datetime.now(timezone.utc).isoformat(),
                                     **metadata, **policy}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(directory / "young.login-status.json")
    stop_if_manual_verification(stage)


def run(timeout_seconds: int = 240):
    login_state("starting", "正在后台连接青春科大统一身份认证。")
    try:
        credentials = load_credentials()
        try:
            device = load_device_state(credentials["username"])
        except BBError:
            device = None
        state = device["state"] if device else {"cookies": [], "origins": []}
        try:
            previous = load_session()["state"]
            state = {key: state[key] + previous[key] for key in ("cookies", "origins")}
        except YoungError:
            pass
        with sync_playwright() as p, chrome_browser(p) as browser:
            options = dict(CONTEXT_OPTIONS)
            if device:
                options.update({k:v for k,v in device.get("context_options",{}).items() if k in {*CONTEXT_OPTIONS,"user_agent"}})
            options.setdefault("user_agent", f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{browser.version} Safari/537.36")
            context = browser.new_context(**options, storage_state=state)
            page = context.new_page()
            page.goto(HOME, wait_until="domcontentloaded", timeout=45000)
            login_state("authenticating", "正在后台复用统一身份和青春科大会话。")
            submitted = False
            verification = EmailVerification(credentials.get("email_verification_enabled") is True, login_state, remember_device, expected_address=credentials.get("email_verification_address"))
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                host = urlsplit(page.url).hostname
                if host in IDENTITY_HOSTS:
                    remember_device(page)
                    if not submitted:
                        submitted = try_submit_credentials(page, credentials)
                        if submitted:
                            login_state("credentials_submitted", "已向学校 HTTPS 认证站点提交一次凭据。")
                    if submitted:
                        verification.step(page)
                        text = page.locator("body").inner_text(timeout=3000)
                        verification.observe_result(text)
                        if re.search(r"密码错误|用户名或密码不正确|Invalid credentials", text, re.I):
                            raise YoungError("学校未接受已保存的凭据，已停止自动重试。")
                        if not verification.last_stage and re.search(r"验证码|动态口令|二次验证|Verification code",text,re.I):
                            login_state("waiting_for_verification", "学校要求人工身份验证。")
                if host == HOST and authenticated_page(page):
                    state = context.storage_state(indexed_db=True)
                    save_session(state, credentials["username"], options["user_agent"])
                    save_device_state(credentials["username"], state, "chrome", options)
                    metadata = {"connected": True, "credential_submission_count": int(submitted), "email_code_requested": verification.requested}
                    login_state("connected", "青春科大登录已验证，会话已加密保存。", **metadata)
                    print(json.dumps(metadata, ensure_ascii=False))
                    return
                page.wait_for_timeout(400)
            raise YoungError("青春科大后台登录等待超时，已有会话仍保留。")
    except BackgroundVerificationRequired:
        return
    except (BBError, YoungError) as exc:
        login_state("error", str(exc))
        raise YoungError(str(exc)) from None
    except PlaywrightError:
        login_state("error", "青春科大后台 Chrome 登录未完成；请确认 Chrome 已安装及网络可用。")
        raise YoungError("青春科大后台登录未完成，请检查本地进度。") from None
