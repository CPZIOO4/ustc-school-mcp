"""Side-effect-free first-use guide and a user-driven local setup operation."""
from __future__ import annotations

import os

from .client import MailClient
from .config import MailConfig, MailError, load_config, local_dir
from .credentials import save_credentials


def local_status() -> dict:
    """Presence is not proof of decryptability or a valid remote login."""
    try:
        load_config()
    except MailError:
        return {"configured": False, "state": "setup_required", "network_checked": False,
                "read_only": True, "next_step": "调用 school_mail_setup_guide 查看本机接入步骤。"}
    available = bool(os.environ.get("SCHOOL_MAIL_PASSWORD")) or (local_dir() / "mail.credentials.dpapi").is_file()
    return {"configured": available, "state": "configured_unchecked" if available else "credential_required",
            "network_checked": False, "credentials_checked": False, "read_only": True,
            "next_step": "调用 school_mail_check_connection 验证连接。" if available else "运行 python setup_mail.py，在本机输入已有客户端专用密码。"}


def setup_guide() -> dict:
    return {**local_status(), "manual_setup_command": "python setup_mail.py",
            "check_command": ".venv\\Scripts\\python.exe -m school_mcp check",
            "webmail_url": "https://mail.ustc.edu.cn/", "help_url": "https://mail.ustc.edu.cn/notice/2fa/",
            "steps": [
                "明确选择本机接入后，运行 python setup_mail.py；工具本身不会弹窗或打开浏览器。",
                "在学校网页邮箱中人工登录并完成学校要求的验证；凭据和验证码不要发送到对话。",
                "使用已授权的客户端专用密码；需要新建时，由本人在设置 → 安全设置 → 客户端专用密码中操作。程序不创建、删除或重置授权。",
                "将完整邮箱地址和客户端专用密码输入本机遮罩窗口；只读 IMAP TLS 验证成功后才加密保存。",
                "接入失败或名额不足时停止，保留已有可用配置；新生成的密码只显示一次，勿反复生成。",
                "调用 school_mail_check_connection 确认可用。若要使用统一身份邮件二次验证，需要另行明确启用该流程。"],
            "automatic_password_creation": False, "opens_window": False,
            "secrets_in_chat": False, "network_checked": False}


def connect_and_save(address: str, password: str) -> dict:
    """Called only by the explicitly opened local form, never an MCP secret parameter."""
    config = MailConfig(address.strip())
    secret = "".join(password.split())
    if not secret:
        raise MailError("请填写客户端专用密码。")
    result = MailClient(config, secret).check()
    try:
        save_credentials(config, secret)
    except OSError:
        raise MailError("邮箱连接已验证，但本机加密保存失败。请保留专用密码，在本机重试保存；不要重新生成授权。") from None
    return {"connected": True, "saved": True, "messages": result["messages"], "unread": result["unread"], "read_only": True}
