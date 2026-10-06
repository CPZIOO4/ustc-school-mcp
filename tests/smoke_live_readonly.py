"""Explicit, minimal live MCP checks. Never reconnects or dumps tool payloads."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CHECKS = (
    ("mail", "serve", "school_mail_check_connection"),
    ("bb", "bb-serve", "school_bb_check_connection"),
    ("jw", "jw-serve", "school_jw_check_connection"),
    ("library", "library-serve", "school_library_check_connection"),
    ("teach", "teach-serve", "school_teach_check_connection"),
    ("nan7", "nan7-serve", "school_nan7_check_connection"),
    ("icourse", "icourse-serve", "school_icourse_check_connection"),
    ("young", "young-serve", "school_young_check_connection"),
)


def error_category(result) -> str:
    text = "\n".join(item.text for item in result.content if getattr(item, "type", None) == "text")
    if "尚未登录或" in text or "会话无法读取" in text or "本地会话无法读取" in text:
        return "local_session_missing_or_unreadable"
    if "已失效" in text or "会话失效" in text or "尚未认证" in text or "没有取得已登录" in text:
        return "session_expired_or_access_denied"
    if "无法读取" in text or "超时" in text or "无法连接" in text:
        return "network_or_response_failure"
    return "user_login_or_network_check_required"


async def check(adapter: str, command: str, name: str) -> dict:
    env = {**os.environ, "PYTHONUTF8": "1", "SCHOOL_MCP_BROWSER_HEADED": "0"}
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "school_mcp", command], env=env)
    summary = {"adapter": adapter, "tool": name, "checked_at": datetime.now(timezone.utc).isoformat(), "read_only": True}
    try:
        # Server stderr can include library diagnostics; do not surface or store it.
        with open(os.devnull, "w", encoding="utf-8") as diagnostics:
            async with stdio_client(parameters, errlog=diagnostics) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=75)) as session:
                    await session.initialize()
                    result = await session.call_tool(name, {})
                    if result.isError:
                        summary.update(outcome="unavailable", reason=error_category(result), next_step="user_login_or_network_check_required")
                    else:
                        data = result.structuredContent
                        if isinstance(data, dict) and data.get("connected") is True:
                            summary.update(outcome="actual_read_success", connected=True)
                            if adapter == "mail":
                                summary["counts_available"] = all(isinstance(data.get(key), int) for key in ("messages", "unread"))
                            if adapter == "library":
                                summary["transport"] = data.get("transport") if data.get("transport") in {"http", "https"} else "unknown"
                            if adapter == "young":
                                summary["headless"] = data.get("headless") is True
                        else:
                            summary.update(outcome="unverified_response")
    except Exception:
        # Do not print exception reprs, auth data, server responses, or account identifiers.
        summary.update(outcome="blocked_or_timeout", next_step="inspect_local_connection_without_exporting_secrets")
    return summary


async def main(adapters: list[str]) -> int:
    failed = False
    for adapter, command, tool in CHECKS:
        if adapters and adapter not in adapters:
            continue
        result = await check(adapter, command, tool)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        failed |= result["outcome"] != "actual_read_success"
    return int(failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Explicitly run one existing-session connection check per selected adapter")
    parser.add_argument("--adapter", choices=[item[0] for item in CHECKS], action="append", default=[])
    args = parser.parse_args()
    if not args.run:
        parser.error("--run is required; this script uses existing local sessions and makes live read-only requests")
    raise SystemExit(asyncio.run(main(args.adapter)))
