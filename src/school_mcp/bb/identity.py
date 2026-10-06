"""User-authorized USTC identity credentials and remembered-device state."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from school_mcp.mail.config import MailError, load_config, local_dir
from school_mcp.mail.credentials import _dpapi

from .session import BB_HOSTS, BBError

IDENTITY_HOSTS = {"id.ustc.edu.cn", "passport.ustc.edu.cn"}
AUTH_HOSTS = BB_HOSTS | IDENTITY_HOSTS
COOKIE_DOMAINS = AUTH_HOSTS | {"ustc.edu.cn", ".ustc.edu.cn"}


def _save(name: str, data: dict) -> None:
    try:
        protected = _dpapi(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        directory = local_dir()
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / (name + ".tmp")
        temporary.write_bytes(protected)
        temporary.replace(directory / name)
    except (MailError, OSError):
        raise BBError("统一身份信息加密保存失败，请使用当前 Windows 用户。") from None


def _load(name: str) -> dict:
    try:
        return json.loads(_dpapi((local_dir() / name).read_bytes(), decrypt=True))
    except (MailError, OSError, ValueError, TypeError):
        raise BBError("统一身份本地配置尚未保存或无法读取。") from None


def save_credentials(username: str, password: str, *, email_verification: bool = False) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,100}", username) or not 1 <= len(password) <= 1024 or "\x00" in password:
        raise BBError("统一身份账号或密码格式无效。")
    data = {"username": username, "password": password, "saved_at": datetime.now(timezone.utc).isoformat(), "email_verification_enabled": email_verification}
    if email_verification:
        data["email_verification_address"] = _verification_address()
    _save("ustc-identity.credentials.dpapi", data)


def _verification_address() -> str:
    try:
        return load_config().address
    except MailError:
        raise BBError("启用邮件验证前，请先在本机接入并验证邮箱。") from None


def load_credentials() -> dict:
    data = _load("ustc-identity.credentials.dpapi")
    if not isinstance(data, dict) or not isinstance(data.get("username"), str) or not isinstance(data.get("password"), str) or not data["password"]:
        raise BBError("统一身份本地凭据格式无效。")
    return data


def set_email_verification(enabled: bool) -> None:
    data = load_credentials()
    data["email_verification_enabled"] = bool(enabled)
    if enabled:
        data["email_verification_address"] = _verification_address()
    else:
        data.pop("email_verification_address", None)
    _save("ustc-identity.credentials.dpapi", data)


def setup_credentials(email_verification: bool = False) -> None:
    from getpass import getpass

    username = input("统一身份账号：").strip()
    password = getpass("统一身份密码（不回显）：")
    save_credentials(username, password, email_verification=email_verification)
    print("统一身份凭据已加密保存；登录时验证学校是否接受这些凭据。")


def scoped_state(state: dict) -> dict:
    cookies = []
    for cookie in state.get("cookies", []):
        if cookie.get("domain", "").lower() not in COOKIE_DOMAINS and cookie.get("domain", "").lstrip(".").lower() not in AUTH_HOSTS:
            continue
        cookies.append(dict(cookie))
    origins = []
    for origin in state.get("origins", []):
        try:
            parsed = urlsplit(origin.get("origin", ""))
            if parsed.scheme == "https" and parsed.hostname in AUTH_HOSTS and parsed.port in (None, 443) and not parsed.username and not parsed.password:
                origins.append(dict(origin))
        except ValueError:
            continue
    return {"cookies": cookies, "origins": origins}


def load_device_state(account: str | None = None) -> dict:
    data = _load("ustc-identity.device.dpapi")
    if not isinstance(data, dict) or not isinstance(data.get("state"), dict):
        raise BBError("统一身份设备状态格式无效。")
    if account is not None and data.get("account") != account:
        raise BBError("设备状态与统一身份账号不一致，请重新认证。")
    data["state"] = scoped_state(data["state"])
    return data


def save_device_state(account: str | None, state: dict, channel: str, context_options: dict) -> dict:
    selected = scoped_state(state)
    # A BB-only refresh must not accidentally discard identity-provider trust cookies.
    try:
        previous = load_device_state(account)
    except BBError:
        previous = None
    if previous is not None:
        cookies = {(c["name"], c["domain"], c["path"]): c for c in previous["state"]["cookies"] if c.get("expires", -1) == -1 or c.get("expires", 0) > time.time()}
        cookies.update({(c["name"], c["domain"], c["path"]): c for c in selected["cookies"]})
        selected["cookies"] = list(cookies.values())
        origins = {origin["origin"]: origin for origin in previous["state"]["origins"]}
        origins.update({origin["origin"]: origin for origin in selected["origins"]})
        selected["origins"] = list(origins.values())
    data = {"account": account, "saved_at": datetime.now(timezone.utc).isoformat(), "channel": channel, "context_options": context_options, "state": selected}
    _save("ustc-identity.device.dpapi", data)
    return data


def status() -> dict:
    result = {"credentials_saved": False, "device_state_saved": False, "identity_cookie_count": 0, "identity_persistent_cookie_count": 0, "email_verification_enabled": False}
    account = None
    try:
        credentials = load_credentials()
        account = credentials["username"]
        result.update(credentials_saved=True)
        result["email_verification_enabled"] = credentials.get("email_verification_enabled") is True
    except BBError:
        pass
    try:
        data = load_device_state(account)
        identity = [c for c in data["state"]["cookies"] if c["domain"].lstrip(".") in IDENTITY_HOSTS | {"ustc.edu.cn"}]
        result.update(device_state_saved=True, device_state_saved_at=data["saved_at"], identity_cookie_count=len(identity), identity_persistent_cookie_count=sum(c.get("expires", -1) > time.time() for c in identity))
    except BBError:
        pass
    return result
