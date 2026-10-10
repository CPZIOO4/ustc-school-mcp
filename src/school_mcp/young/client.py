from __future__ import annotations

import re
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from ..bb.login import CONTEXT_OPTIONS
from ..browser import chrome_browser
from .. import network
from ..service_errors import identity_redirect, policy_error
from .session import HOME, HOST, YoungError, load_session


def authenticated_page(page) -> bool:
    parsed = urlsplit(page.url)
    return (parsed.scheme == "https" and parsed.hostname == HOST and parsed.port in (None,443)
            and not parsed.username and not parsed.password
            and page.get_by_text("欢迎您", exact=False).count() > 0)


class YoungClient:
    def home(self, max_chars: int = 4000) -> dict:
        if isinstance(max_chars,bool) or not isinstance(max_chars,int) or not 1 <= max_chars <= 50000:
            raise YoungError("max_chars 必须为 1–50000 的整数。")
        session = load_session()
        options = dict(CONTEXT_OPTIONS)
        if session.get("user_agent"):
            options["user_agent"] = session["user_agent"]
        try:
            network.limiter.acquire("young")
            with sync_playwright() as p, chrome_browser(p, headless=True) as browser:
                context = browser.new_context(**options, storage_state=session["state"])
                page = context.new_page()
                response = page.goto(HOME, wait_until="domcontentloaded", timeout=30000)
                if response is not None:
                    network.limiter.response("young", response.status, response.headers)
                    if response.status in {401, 403}:
                        raise YoungError("青春科大会话失效或访问受限。", code='authentication_required' if response.status == 401 else 'access_denied')
                if identity_redirect(page.url):
                    raise YoungError('青春科大需要认证。', code='authentication_required')
                page.get_by_text("欢迎您", exact=False).first.wait_for(timeout=15000)
                if not authenticated_page(page):
                    raise YoungError("青春科大会话已失效，请调用 school_young_reconnect。")
                # The shell contains empty labels and NaN placeholders before its API returns.
                page.get_by_text(re.compile(r"数据更新截至时间\s*\d{4}年")).first.wait_for(timeout=15000)
                text = page.locator("body").inner_text(timeout=5000)
                network.limiter.success("young")
                return {"connected": True, "title": page.title(), "source": HOME, "text": text[:max_chars],
                        "text_truncated": len(text) > max_chars, "headless": True, "content_is_untrusted": True,
                        "note": "数据大屏包含全校汇总，不能当作本人学时或本人活动记录。"}
        except network.PolicyError as exc:
            raise policy_error(YoungError, exc) from None
        except PlaywrightError:
            network.limiter.failure("young")
            raise YoungError("青春科大后台页面读取未完成，可能会话失效或页面超时；请检查状态后重新连接。") from None

    def check(self) -> dict:
        result = self.home(1)
        return {k: result[k] for k in ("connected", "title", "source", "headless")}
