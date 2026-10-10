from __future__ import annotations

import json
from datetime import datetime, timezone

from ..bb.identity import load_credentials
from ..bb.session import BBError
from ..mail.config import MailError, local_dir
from ..mail.credentials import _dpapi

SITE_URL = "https://nan7market.com"
API_URL = "https://nan7market-api.0x01.work/api"
LOGIN_URL = "https://sso-proxy.lug.ustc.edu.cn/auth/default/?service=https%3A%2F%2Fnan7market.com%2Fcas"


from ..service_errors import ServiceError


class Nan7Error(ServiceError):
    """A credential-free error safe to show to the MCP client."""


def valid_token(token: object) -> bool:
    return isinstance(token, str) and 1 <= len(token) <= 16384 and token not in {"null", "undefined"} and all(33 <= ord(c) <= 126 for c in token)


def save_session(token: str, account: str | None) -> None:
    if not valid_token(token):
        raise Nan7Error("没有取得有效的南七集市会话。")
    data = {"token": token, "account": account, "saved_at": datetime.now(timezone.utc).isoformat()}
    try:
        directory = local_dir()
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "nan7-session.tmp"
        temporary.write_bytes(_dpapi(json.dumps(data).encode("utf-8")))
        temporary.replace(directory / "nan7-session.dpapi")
    except (MailError, OSError):
        raise Nan7Error("南七集市会话加密保存失败，请使用当前 Windows 用户。") from None


def load_session() -> dict:
    try:
        data = json.loads(_dpapi((local_dir() / "nan7-session.dpapi").read_bytes(), decrypt=True))
        if not isinstance(data, dict) or not valid_token(data.get("token")) or not isinstance(data.get("saved_at"), str):
            raise ValueError("invalid session")
        try:
            account = load_credentials()["username"]
        except BBError:
            account = None
        if account is not None and data.get("account") != account:
            raise Nan7Error("南七集市会话与当前统一身份账号不一致，请调用 school_nan7_reconnect。", code='authentication_required')
        return data
    except Nan7Error:
        raise
    except (MailError, OSError, ValueError, TypeError, KeyError):
        raise Nan7Error("南七集市尚未登录或会话无法读取，请调用 school_nan7_reconnect。", code='authentication_required') from None
