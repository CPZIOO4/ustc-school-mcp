"""Side-effect-free first-use guide and a user-driven local setup operation."""
from __future__ import annotations

import os
import sys

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
            "browser_setup_command": "python -m school_mcp mail-bind --headed --create-client-password",
            "resume_command": "python -m school_mcp mail-bind --resume",
            "browser_setup_argv": [sys.executable, "-m", "school_mcp", "mail-bind", "--headed", "--create-client-password"],
            "resume_argv": [sys.executable, "-m", "school_mcp", "mail-bind", "--resume"],
            "required_environment": {"SCHOOL_MCP_LOCAL_DIR": str(local_dir()), "PYTHONUTF8": "1"},
            "status_tool": "school_mail_bind_status",
            "check_command": ".venv\\Scripts\\python.exe -m school_mcp check",
            "webmail_url": "https://mail.ustc.edu.cn/", "help_url": "https://mail.ustc.edu.cn/notice/2fa/",
            "steps": [
                "用户选择接入邮箱并创建专用密码后，在已安装的项目环境运行 browser_setup_command；此引导工具本身不弹窗。",
                "用户在单独的 Chrome 窗口中登录学校邮箱并完成校方验证；主密码、客户端密码和验证码都不要发送到对话。",
                "程序在可识别页面上创建一个 SchoolMCP 专用密码，先加密暂存，再只读 IMAP 验证并保存；真实页面流程仍待用户验收。",
                "用 school_mail_bind_status 查看进度；captured 用 resume_command 恢复，creation_attempted 不得重新生成。程序不删除或重置其他授权。",
                "需要人工处理时，按 docs/mail.md 操作；已有专用密码可用 python setup_mail.py 在本机遮罩窗口录入。",
                "调用 school_mail_check_connection 确认可用。若要使用统一身份邮件二次验证，需要另行明确启用该流程。"],
            "automatic_password_creation": True, "automatic_creation_live_verified": False, "opens_window": False,
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
