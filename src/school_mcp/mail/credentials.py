"""Windows user-bound DPAPI storage; no plaintext credential files."""

from __future__ import annotations

import ctypes
import json
import os
import tempfile
from pathlib import Path
from ctypes import wintypes

from .config import MailConfig, MailError, local_dir


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise MailError("本地加密凭据目前支持 Windows；其他系统可使用 SCHOOL_MAIL_PASSWORD 环境变量。")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    api.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    api.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _Blob()
    # CRYPTPROTECT_UI_FORBIDDEN. The default binds the ciphertext to this Windows user.
    if not api(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise MailError("Windows 凭据加密或解密失败，请在当前 Windows 用户下重新配置。")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


def save_credentials(config: MailConfig, password: str) -> None:
    if not isinstance(password, str) or not password or any(c in password for c in "\r\n\x00"):
        raise MailError("客户端专用密码不能为空。")
    payload = json.dumps({"address": config.address, "password": password}, ensure_ascii=False).encode("utf-8")
    ciphertext = _dpapi(payload)
    directory = local_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # Stage BOTH files before committing. Keep encrypted backups for rollback;
    # a failed config replacement must not invalidate a previously working mailbox.
    staged = Path(tempfile.mkdtemp(prefix="mail-setup-", dir=directory))
    committed = []
    keep_backup = False
    names = ("mail.credentials.dpapi", "mail.json")
    try:
        (staged / (names[0] + ".new")).write_bytes(ciphertext)
        (staged / (names[1] + ".new")).write_text(json.dumps({"address": config.address}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for name in names:
            target = directory / name
            if target.exists():
                (staged / (name + ".old")).write_bytes(target.read_bytes())
        for name in names:
            (staged / (name + ".new")).replace(directory / name)
            committed.append(name)
    except OSError:
        try:
            for name in reversed(committed):
                backup = staged / (name + ".old")
                if backup.exists():
                    backup.replace(directory / name)
                else:
                    (directory / name).unlink(missing_ok=True)
        except OSError:
            keep_backup = True
            raise MailError("保存和恢复旧配置均未完成。私人目录中的 mail-setup-* 保留了加密备份，请在本机处理；不要生成新授权。") from None
        raise MailError("本机保存失败，已保留旧配置。请保留专用密码并在本机重试，不要生成新授权。") from None
    finally:
        if not keep_backup:
            for name in names:
                for suffix in (".new", ".old"):
                    try:
                        (staged / (name + suffix)).unlink(missing_ok=True)
                    except OSError:
                        pass
            try:
                staged.rmdir()
            except OSError:
                pass


def load_password(config: MailConfig) -> str:
    password = os.environ.get("SCHOOL_MAIL_PASSWORD")
    if password:
        return password
    try:
        payload = json.loads(_dpapi((local_dir() / "mail.credentials.dpapi").read_bytes(), decrypt=True))
        if payload["address"] != config.address or not isinstance(payload["password"], str) or not payload["password"]:
            raise MailError("本地凭据与邮箱地址不一致，请重新配置。")
        return payload["password"]
    except (OSError, ValueError, KeyError, TypeError):
        raise MailError("尚未保存客户端专用密码，请运行本地邮箱接入窗口。") from None
