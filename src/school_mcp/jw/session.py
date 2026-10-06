from __future__ import annotations

import json
from datetime import datetime, timezone

from ..mail.config import MailError, local_dir
from ..mail.credentials import _dpapi

BASE_URL = "https://jw.ustc.edu.cn"
HOST = "jw.ustc.edu.cn"


class JWError(Exception):
    """A message safe to return to the MCP client."""


def jw_cookies(cookies: list[dict]) -> list[dict]:
    return [dict(cookie) for cookie in cookies if cookie.get("domain", "").lstrip(".").lower() in {HOST, "ustc.edu.cn"}]


def save_session(cookies: list[dict], user_agent: str) -> None:
    selected = jw_cookies(cookies)
    if not selected:
        raise JWError("没有取得教务系统会话，请先完成统一身份认证。")
    data = {"base_url": BASE_URL, "cookies": selected, "user_agent": user_agent, "saved_at": datetime.now(timezone.utc).isoformat()}
    try:
        directory = local_dir()
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "jw.session.tmp"
        temporary.write_bytes(_dpapi(json.dumps(data, ensure_ascii=False).encode("utf-8")))
        temporary.replace(directory / "jw.session.dpapi")
    except (MailError, OSError):
        raise JWError("教务系统会话加密保存失败，请使用当前 Windows 用户。") from None


def load_session() -> dict:
    try:
        data = json.loads(_dpapi((local_dir() / "jw.session.dpapi").read_bytes(), decrypt=True))
        if data["base_url"] != BASE_URL or not jw_cookies(data["cookies"]) or not isinstance(data["user_agent"], str):
            raise ValueError("invalid session")
        data["cookies"] = jw_cookies(data["cookies"])
        return data
    except (MailError, OSError, ValueError, TypeError, KeyError):
        raise JWError("教务系统尚未登录或会话无法读取，请调用 school_jw_reconnect。") from None
