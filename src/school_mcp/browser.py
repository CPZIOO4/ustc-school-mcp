"""Shared background browser policy for school adapters (no browser extension)."""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager


class BackgroundVerificationRequired(Exception):
    """Login stopped without opening a visible window; progress is already saved."""


def is_headless() -> bool:
    return os.environ.get("SCHOOL_MCP_BROWSER_HEADED") != "1"


def _native_call(operation, value):
    from .desktop import native_core
    from .desktop.model import ModelError
    from .service_errors import ServiceError
    try:
        return native_core.call(operation, value)
    except ModelError as exc:
        code = {"browser_busy": "login_busy", "invalid_arguments": "invalid_arguments"}.get(exc.code, "runtime_failure")
        messages = {
            "runtime_failure": "后台 Chrome 运行异常；请检查本机 Chrome 和程序状态后继续。",
            "login_busy": "后台浏览器已达并发上限，请先结束已有任务。",
            "invalid_arguments": "后台浏览器请求参数无效。",
        }
        raise ServiceError(messages[code], code=code) from None


@contextmanager
def chrome_browser(playwright, *, headless: bool | None = None):
    from .desktop import native_core
    resolved_headless = is_headless() if headless is None else headless
    if not isinstance(resolved_headless, bool):
        raise ValueError("headless must be a boolean")
    if native_core.enabled():
        owned = _native_call("browser_start", {"headless": resolved_headless})
        browser = None
        try:
            browser = playwright.chromium.connect_over_cdp(owned["endpoint"], timeout=15000)
            yield browser
        finally:
            original_failure = sys.exc_info()[0] is not None
            try:
                if browser is not None and browser.is_connected():
                    browser.close()
            except Exception:
                if not original_failure:
                    raise
            finally:
                try:
                    _native_call("browser_stop", {"browser_id": owned["browser_id"]})
                except Exception:
                    if not original_failure:
                        raise
        return
    browser = playwright.chromium.launch(channel="chrome", headless=resolved_headless)
    try:
        yield browser
    finally:
        if browser.is_connected():
            browser.close()


def background_progress(stage: str, detail: str) -> tuple[str, dict]:
    from . import network
    if stage in {"error", "cancelled", "waiting_for_verification"}:
        try:
            network.limiter.failure("identity", authentication=True)
        except network.PolicyError:
            pass  # Preserve the original terminal login status; future starts fail closed.
    elif stage == "connected":
        try:
            network.limiter.success("identity")
        except network.PolicyError:
            pass
    metadata = {"browser_channel": "chrome", "headless": is_headless()}
    if stage == "waiting_for_verification" and is_headless():
        # Callers supply fixed, secret-free reasons. Keep the actionable reason.
        detail += " 后台登录已停止，未打开窗口；可明确选择 --headed 模式完成人工验证。"
        metadata["requires_user_action"] = True
    return detail, metadata


def stop_if_manual_verification(stage: str) -> None:
    if stage == "waiting_for_verification" and is_headless():
        raise BackgroundVerificationRequired()
