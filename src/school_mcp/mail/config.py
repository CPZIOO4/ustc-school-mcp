from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


class MailError(Exception):
    """An error safe to return to the MCP client."""


def local_dir() -> Path:
    configured = os.environ.get("SCHOOL_MCP_LOCAL_DIR")
    return Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parents[3] / ".local"


@dataclass(frozen=True)
class MailConfig:
    address: str
    host: str = "mail.ustc.edu.cn"
    port: int = 993
    timeout: int = 15

    def __post_init__(self) -> None:
        if not isinstance(self.address, str):
            raise MailError("请输入完整的中科大邮箱地址。")
        if any(c.isspace() or c in '\r\n\x00' for c in self.address):
            raise MailError("邮箱地址包含无效字符。")
        name, separator, domain = self.address.partition("@")
        if not name or len(self.address) > 254 or separator != "@" or domain.lower() not in {"mail.ustc.edu.cn", "ustc.edu.cn"}:
            raise MailError("请输入完整的中科大邮箱地址。")
        if self.host != "mail.ustc.edu.cn" or self.port != 993:
            raise MailError("此适配器仅连接 mail.ustc.edu.cn 的 IMAP TLS 993 端口。")


def load_config() -> MailConfig:
    try:
        data = json.loads((local_dir() / "mail.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        return MailConfig(address=data["address"])
    except (OSError, ValueError, KeyError, TypeError):
        raise MailError("邮箱尚未配置，请运行本地邮箱接入窗口。") from None
