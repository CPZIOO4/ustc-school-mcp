"""Exercise actual stdio entry points without using personal credentials."""

import asyncio
import json
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def check_minimal_defaults(tools):
    expected = {
        "school_mail_search": {"limit": 10, "include_headers": False},
        "school_mail_read": {"max_chars": 6000, "include_headers": False},
        "school_jw_grades": {"summary_only": False},
        "school_library_summary": {"include_card_dates": False},
        "school_library_list_loans": {"include_identifiers": False},
        "school_nan7_get_offer": {"include_seller": False},
        "school_bb_read_course": {"max_chars": 6000},
        "school_young_read_home": {"max_chars": 4000},
    }
    for tool in tools.tools:
        for key, default in expected.get(tool.name, {}).items():
            assert tool.inputSchema["properties"][key]["default"] == default, (tool.name, key)


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        env = {**os.environ, "SCHOOL_MCP_LOCAL_DIR": directory, "SCHOOL_MAIL_PASSWORD": "", "PYTHONUTF8": "1"}
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "school_mcp"], env=env)
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
                check_minimal_defaults(tools)
                names = {tool.name for tool in tools.tools}
                assert names == {"school_mail_status", "school_mail_setup_guide", "school_mail_check_connection", "school_mail_list_folders", "school_mail_search", "school_mail_read", "school_mail_download_attachment", "school_mail_prepare", "school_mail_prepare_reply", "school_mail_send", "school_mail_send_status", "school_mail_find_replies", "school_mail_check_sent_copy", "school_mail_save_sent_copy"}, names
                status = await session.call_tool("school_mail_status", {})
                assert status.isError is False, status
                assert status.structuredContent["configured"] is False, status
                guide = await session.call_tool("school_mail_setup_guide", {})
                assert guide.isError is False and guide.structuredContent["opens_window"] is False
                assert guide.structuredContent["network_checked"] is False
                unconfigured = await session.call_tool("school_mail_search", {})
                assert unconfigured.isError is True, unconfigured
                for tool in tools.tools:
                    assert tool.annotations.readOnlyHint is (tool.name not in {"school_mail_download_attachment", "school_mail_prepare", "school_mail_prepare_reply", "school_mail_send", "school_mail_save_sent_copy"})
                sending = next(t for t in tools.tools if t.name == 'school_mail_send')
                assert set(sending.inputSchema['required']) == {'draft_id', 'content_sha256'}
                print(json.dumps({"server": initialized.serverInfo.name, "protocol": initialized.protocolVersion, "tools": sorted(names), "unconfigured_status": status.structuredContent, "missing_credential_error": "handled"}, ensure_ascii=False))
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "school_mcp", "bb-serve"], env=env)
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
                check_minimal_defaults(tools)
                names = {tool.name for tool in tools.tools}
                assert names == {"school_bb_status", "school_bb_check_connection", "school_bb_list_courses", "school_bb_read_course", "school_bb_read_page", "school_bb_course_announcements", "school_bb_auth_status", "school_bb_reconnect"}, names
                status = await session.call_tool("school_bb_status", {})
                assert status.isError is False and status.structuredContent["configured"] is False
                missing = await session.call_tool("school_bb_list_courses", {})
                assert missing.isError is True
                assert all(tool.annotations.readOnlyHint is (tool.name != "school_bb_reconnect") for tool in tools.tools)
                auth = await session.call_tool("school_bb_auth_status", {})
                assert auth.isError is False and auth.structuredContent["credentials_saved"] is False
                print(json.dumps({"server": initialized.serverInfo.name, "tools": sorted(names), "unconfigured_status": "handled"}, ensure_ascii=False))
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "school_mcp", "jw-serve"], env=env)
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
                check_minimal_defaults(tools)
                names = {tool.name for tool in tools.tools}
                assert names == {"school_jw_status", "school_jw_reconnect", "school_jw_check_connection", "school_jw_read_home", "school_jw_list_modules", "school_jw_list_semesters", "school_jw_list_courses", "school_jw_timetable", "school_jw_grades"}, names
                status = await session.call_tool("school_jw_status", {})
                assert status.isError is False and status.structuredContent["configured"] is False
                assert status.structuredContent["credentials_saved"] is False
                missing = await session.call_tool("school_jw_list_courses", {})
                assert missing.isError is True
                assert all(tool.annotations.readOnlyHint is (tool.name != "school_jw_reconnect") for tool in tools.tools)
                print(json.dumps({"server": initialized.serverInfo.name, "tools": sorted(names), "unconfigured_status": "handled"}, ensure_ascii=False))


        parameters = StdioServerParameters(command=sys.executable, args=["-m", "school_mcp", "library-serve"], env=env)
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
                check_minimal_defaults(tools)
                names = {tool.name for tool in tools.tools}
                assert names == {"school_library_status", "school_library_reconnect", "school_library_check_connection", "school_library_summary", "school_library_list_loans", "school_library_loan_history", "school_library_list_services"}, names
                status = await session.call_tool("school_library_status", {})
                assert status.isError is False and status.structuredContent["configured"] is False
                assert status.structuredContent["credentials_saved"] is False
                missing = await session.call_tool("school_library_list_loans", {})
                assert missing.isError is True
                assert all(tool.annotations.readOnlyHint is (tool.name != "school_library_reconnect") for tool in tools.tools)
                print(json.dumps({"server": initialized.serverInfo.name, "tools": sorted(names), "unconfigured_status": "handled"}, ensure_ascii=False))


        for adapter in ("teach", "nan7", "icourse", "young"):
            parameters = StdioServerParameters(command=sys.executable, args=["-m", "school_mcp", f"{adapter}-serve"], env=env)
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    initialized = await session.initialize()
                    tools = await session.list_tools()
                    check_minimal_defaults(tools)
                    names = {tool.name for tool in tools.tools}
                    status_name = f"school_{adapter}_status"
                    assert status_name in names and len(names) >= 2, names
                    assert all(name.startswith(f"school_{adapter}_") for name in names), names
                    assert all(tool.annotations and tool.annotations.readOnlyHint is (tool.name != f"school_{adapter}_reconnect") for tool in tools.tools)
                    assert all(tool.outputSchema is not None for tool in tools.tools)
                    status = await session.call_tool(status_name, {})
                    assert status.isError is False and isinstance(status.structuredContent, dict)
                    if adapter == "young":
                        assert names == {"school_young_status", "school_young_reconnect", "school_young_check_connection", "school_young_read_home"}
                        assert status.structuredContent["configured"] is False
                        missing = await session.call_tool("school_young_read_home", {})
                        assert missing.isError is True
                    print(json.dumps({"server": initialized.serverInfo.name, "tools": sorted(names), "public_adapter_status": "handled"}, ensure_ascii=False))


asyncio.run(main())
