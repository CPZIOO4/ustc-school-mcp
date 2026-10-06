"""Shared background browser policy for school adapters (no browser extension)."""
from __future__ import annotations

import os
from contextlib import contextmanager


class BackgroundVerificationRequired(Exception):
    """Login stopped without opening a visible window; progress is already saved."""


def is_headless() -> bool:
    return os.environ.get("SCHOOL_MCP_BROWSER_HEADED") != "1"


@contextmanager
def chrome_browser(playwright, *, headless: bool | None = None):
    browser = playwright.chromium.launch(channel="chrome", headless=is_headless() if headless is None else headless)
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
        detail = "需要人工身份验证，后台登录已停止，未打开窗口。请明确选择人工登录后使用 --headed 模式。"
        metadata["requires_user_action"] = True
    return detail, metadata


def stop_if_manual_verification(stage: str) -> None:
    if stage == "waiting_for_verification" and is_headless():
        raise BackgroundVerificationRequired()
