from __future__ import annotations

import json
from datetime import datetime, timezone

from school_mcp.mail.config import MailError, local_dir
from school_mcp.mail.credentials import _dpapi

BASE_URL = "https://www.bb.ustc.edu.cn"
PORTAL_PATH = "/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_1_1"
BB_HOSTS = {"www.bb.ustc.edu.cn", "bb.ustc.edu.cn"}


from ..service_errors import ServiceError


class BBError(ServiceError):
    """An error safe to show to the MCP client."""


def bb_cookies(cookies: list[dict]) -> list[dict]:
    selected = []
    for cookie in cookies:
        domain = cookie.get("domain", "").lstrip(".").lower()
        if domain and any(host == domain or host.endswith("." + domain) for host in BB_HOSTS):
            selected.append({key: cookie[key] for key in ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite") if key in cookie})
    return selected


def save_session(cookies: list[dict]) -> None:
    selected = bb_cookies(cookies)
    if not selected:
        raise BBError("没有取得 BB 平台的登录 Cookie，请先完成学校页面中的登录。")
    data = {"base_url": BASE_URL, "saved_at": datetime.now(timezone.utc).isoformat(), "cookies": selected}
    try:
        encrypted = _dpapi(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        directory = local_dir()
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "bb.session.tmp"
        temporary.write_bytes(encrypted)
        temporary.replace(directory / "bb.session.dpapi")
    except (MailError, OSError):
        raise BBError("BB 登录会话加密保存失败，请在当前 Windows 用户下重新登录。") from None


def load_session() -> dict:
    try:
        data = json.loads(_dpapi((local_dir() / "bb.session.dpapi").read_bytes(), decrypt=True))
        if data["base_url"] != BASE_URL or not bb_cookies(data["cookies"]):
            raise ValueError("invalid session")
        return data
    except (MailError, OSError, ValueError, KeyError, TypeError):
        raise BBError("BB 尚未登录或本地会话无法读取，请运行 BB 本地登录窗口。", code='authentication_required') from None
