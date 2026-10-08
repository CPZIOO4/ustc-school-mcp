"""Read-only connection report, omitting personal response payloads and secrets."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from importlib import import_module

SERVICES = ("mail", "bb", "jw", "library", "teach", "nan7", "icourse", "young", "finance")
CLIENTS = {"bb": "BBClient", "jw": "JWClient", "library": "LibraryClient", "teach": "TeachClient",
           "nan7": "Nan7Client", "icourse": "ICourseClient", "young": "YoungClient", "finance": "FinanceClient"}


def check_service(service: str) -> dict:
    started = time.monotonic()
    report = {"service": service, "checked_at": datetime.now(timezone.utc).isoformat(), "read_only": True}
    try:
        if service == "mail":
            from .mail.client import MailClient
            from .mail.config import load_config
            client = MailClient(load_config())
        else:
            client = getattr(import_module(f"school_mcp.{service}.client"), CLIENTS[service])()
        result = client.check()
        report.update(connected=result.get("connected") is True, state="connected" if result.get("connected") is True else "unverified")
        if service == "library":
            report["transport"] = result.get("transport") if result.get("transport") in {"http", "https"} else "unknown"
        if report["connected"] is not True:
            report["next_tool"] = f"school_{service}_check_connection"
            report["note"] = "接口未确认可读；请查看该服务检查工具的具体原因，不能解释为零条数据。"
    except Exception:
        # Errors may originate in third-party packages and contain request details.
        # The service-specific tool exposes a controlled actionable error on demand.
        report.update(connected=False, state="unavailable",
                      next_tool=f"school_{service}_check_connection")
        if service == "mail":
            report["setup_tool"] = "school_mail_setup_guide"
        elif service in {"bb", "jw", "library", "nan7", "young", "finance"}:
            report["reconnect_tool"] = f"school_{service}_reconnect"
            report["note"] = "先用检查工具区分网络问题与登录失效；已有登录授权时可后台重连。"
        else:
            report["note"] = "公共读取无需邮箱或统一身份登录，请检查网络或站点状态。"
    report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return report


def diagnose(services: list[str] | None = None) -> dict:
    selected = list(dict.fromkeys(services or SERVICES))
    if any(name not in SERVICES for name in selected):
        raise ValueError("Unsupported school service")
    # No reconnects: independent read-only checks can run concurrently.
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(check_service, selected))
    return {"all_connected": all(item["connected"] for item in results), "checks": results,
            "note": "仅验证当前连接，不自动重新认证、不输出邮件、课程、成绩或借阅内容。"}
