from __future__ import annotations

import json
from datetime import datetime, timezone

from ..mail.config import MailError, local_dir
from ..mail.credentials import _dpapi

PUBLIC_URL = "https://lib.ustc.edu.cn"
HOST = "opac.lib.ustc.edu.cn"
BASE_URLS = {"https://" + HOST, "http://" + HOST}


class LibraryError(Exception):
    """A message safe to return to the MCP client."""


def library_cookies(cookies: list[dict]) -> list[dict]:
    return [dict(c) for c in cookies if c.get("domain", "").lstrip(".").lower() in {HOST, "lib.ustc.edu.cn"}]


def save_session(cookies: list[dict], base_url: str, user_agent: str, account: str | None) -> None:
    selected = library_cookies(cookies)
    if base_url not in BASE_URLS or not selected:
        raise LibraryError("没有取得有效的个人图书馆会话。")
    data = {"cookies": selected, "base_url": base_url, "user_agent": user_agent, "account": account, "saved_at": datetime.now(timezone.utc).isoformat()}
    try:
        directory = local_dir()
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "library.session.tmp"
        temporary.write_bytes(_dpapi(json.dumps(data, ensure_ascii=False).encode("utf-8")))
        temporary.replace(directory / "library.session.dpapi")
    except (MailError, OSError):
        raise LibraryError("图书馆会话加密保存失败，请使用当前 Windows 用户。") from None


def load_session() -> dict:
    try:
        data = json.loads(_dpapi((local_dir() / "library.session.dpapi").read_bytes(), decrypt=True))
        if not isinstance(data, dict) or data["base_url"] not in BASE_URLS or not isinstance(data["cookies"], list) or not isinstance(data["user_agent"], str):
            raise ValueError("invalid session")
        data["cookies"] = library_cookies(data["cookies"])
        if not data["cookies"]:
            raise ValueError("no scoped cookies")
        from ..bb.identity import load_credentials
        from ..bb.session import BBError
        try:
            account = load_credentials()["username"]
        except BBError:
            account = None
        if account is not None and data.get("account") != account:
            raise LibraryError("图书馆会话与当前统一身份账号不一致，请重新登录。")
        return data
    except LibraryError:
        raise
    except (MailError, OSError, ValueError, TypeError, KeyError, AttributeError):
        raise LibraryError("个人图书馆尚未登录或会话无法读取，请调用 school_library_reconnect。") from None
