"""Exercise actual stdio entry points without using personal credentials."""

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXTRA_MAIL_TOOLS = {
    'school_mail_schedule_workflow', 'school_mail_script_status', 'school_mail_cancel_script',
    'school_mail_prepare_forward', 'school_mail_prepare_reply_all', 'school_mail_create_folder',
    'school_mail_prepare_actions', 'school_mail_execute_actions', 'school_mail_action_status',
    'school_mail_prepare_undo', 'school_mail_export', 'school_mail_get_classification_rules',
    'school_mail_save_classification_rules', 'school_mail_preview_classification',
    'school_mail_prepare_classification', 'school_mail_read_thread',
}
EXTRA_MAIL_WRITES = EXTRA_MAIL_TOOLS - {'school_mail_script_status', 'school_mail_action_status', 'school_mail_get_classification_rules', 'school_mail_read_thread'}


def check_minimal_defaults(tools):
    expected = {
        "school_mail_search": {"limit": 10, "include_headers": False},
        "school_mail_read": {"max_chars": 6000, "include_headers": False},
        "school_mail_list_drafts": {"state": "ready", "limit": 10, "include_recipients": False},
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
                assert names == EXTRA_MAIL_TOOLS | {"school_mail_status", "school_mail_setup_guide", "school_mail_check_connection", "school_mail_list_folders", "school_mail_search", "school_mail_read", "school_mail_download_attachment", "school_mail_prepare", "school_mail_prepare_reply", "school_mail_send", "school_mail_send_status", "school_mail_find_replies", "school_mail_check_sent_copy", "school_mail_save_sent_copy", "school_mail_list_drafts", "school_mail_update_draft", "school_mail_cancel_draft"}, names
                status = await session.call_tool("school_mail_status", {})
                assert status.isError is False, status
                assert status.structuredContent["configured"] is False, status
                guide = await session.call_tool("school_mail_setup_guide", {})
                assert guide.isError is False and guide.structuredContent["opens_window"] is False
                assert guide.structuredContent["network_checked"] is False
                unconfigured = await session.call_tool("school_mail_search", {})
                assert unconfigured.isError is True, unconfigured
                for tool in tools.tools:
                    assert tool.annotations.readOnlyHint is (tool.name not in EXTRA_MAIL_WRITES | {"school_mail_download_attachment", "school_mail_prepare", "school_mail_prepare_reply", "school_mail_send", "school_mail_save_sent_copy", "school_mail_update_draft", "school_mail_cancel_draft"})
                sending = next(t for t in tools.tools if t.name == 'school_mail_send')
                assert set(sending.inputSchema['required']) == {'draft_id', 'content_sha256'}
                listing = next(t for t in tools.tools if t.name == 'school_mail_list_drafts')
                assert set(listing.inputSchema['properties']['state']['enum']) == {'ready', 'cancelled', 'superseded', 'all'}
                if os.name == 'nt':
                    # Actual protocol + DPAPI lifecycle in a separate synthetic mailbox.
                    # No SMTP credentials exist. No valid ready draft is ever sent.
                    (Path(directory) / 'mail.json').write_text(json.dumps({'address': 'student@mail.ustc.edu.cn'}), encoding='utf-8')
                    async def call(name, args):
                        result = await session.call_tool(name, args)
                        assert not result.isError, result
                        return result.structuredContent
                    original = await call('school_mail_prepare', {'to': ['recipient@example.com'], 'subject': 'synthetic lifecycle', 'body': 'synthetic first body'})
                    start = datetime.now(timezone.utc) + timedelta(hours=1)
                    arguments = {'request': {'kind': 'mail_send', 'draft_id': original['draft_id'], 'content_sha256': original['content_sha256']},
                                 'schedule': {'start_at': start.isoformat(), 'end_at': (start+timedelta(hours=1)).isoformat()},
                                 'authorized': True}
                    scheduled = await call('school_mail_schedule_workflow', arguments)
                    assert scheduled['worker_running'] and not scheduled['uses_ai']
                    assert scheduled['job_id'] == (await call('school_mail_schedule_workflow', arguments))['job_id']
                    cancelled_job = await call('school_mail_cancel_script', {'job_id': scheduled['job_id']})
                    assert cancelled_job['state'] == 'cancelled'
                    for _ in range(80):
                        job_status = await call('school_mail_script_status', {'job_id': scheduled['job_id']})
                        if not job_status['worker_running']:
                            break
                        await asyncio.sleep(.1)
                    assert not job_status['worker_running'], job_status
                    assert (await call('school_mail_send_status', {'draft_id': original['draft_id']}))['status'] == 'ready'
                    print(json.dumps({'synthetic_background_schedule_cancel': 'passed', 'SMTP_attempted': False}))
                    listed = await call('school_mail_list_drafts', {})
                    assert listed['drafts'][0]['draft_id'] == original['draft_id']
                    revised = await call('school_mail_update_draft', {'draft_id': original['draft_id'], 'content_sha256': original['content_sha256'], 'body': 'synthetic revised body'})
                    assert revised['mutation_applied'] and revised['draft_id'] != original['draft_id']
                    blocked = await call('school_mail_send', {'draft_id': original['draft_id'], 'content_sha256': original['content_sha256']})
                    assert blocked['status'] == 'superseded' and not blocked['can_send']
                    cancelled = await call('school_mail_cancel_draft', {'draft_id': revised['draft_id'], 'content_sha256': revised['content_sha256']})
                    assert cancelled['status'] == 'cancelled' and cancelled['mutation_applied']
                    blocked = await call('school_mail_send', {'draft_id': revised['draft_id'], 'content_sha256': revised['content_sha256']})
                    assert blocked['status'] == 'cancelled' and not blocked['can_send']
                    assert not (await call('school_mail_list_drafts', {}))['drafts']
                    assert len((await call('school_mail_list_drafts', {'state': 'all'}))['drafts']) == 2
                    print(json.dumps({'synthetic_draft_lifecycle': 'passed', 'SMTP_attempted': False}))
                print(json.dumps({"server": initialized.serverInfo.name, "protocol": initialized.protocolVersion, "tools": sorted(names), "unconfigured_status": status.structuredContent, "missing_credential_error": "handled"}, ensure_ascii=False))
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "school_mcp", "bb-serve"], env=env)
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
                check_minimal_defaults(tools)
                names = {tool.name for tool in tools.tools}
                assert names == {"school_bb_schedule_submission", "school_bb_prepare_named_submission", "school_bb_script_status", "school_bb_cancel_script", "school_bb_status", "school_bb_check_connection", "school_bb_list_courses", "school_bb_read_course", "school_bb_read_page", "school_bb_course_announcements", "school_bb_auth_status", "school_bb_reconnect", "school_bb_list_assignments", "school_bb_prepare_assignment_files", "school_bb_inspect_assignment", "school_bb_prepare_submission", "school_bb_submit_assignment", "school_bb_submission_status"}, names
                status = await session.call_tool("school_bb_status", {})
                assert status.isError is False and status.structuredContent["configured"] is False
                missing = await session.call_tool("school_bb_list_courses", {})
                assert missing.isError is True
                assert all(tool.annotations.readOnlyHint is (tool.name not in {"school_bb_schedule_submission", "school_bb_prepare_named_submission", "school_bb_cancel_script", "school_bb_reconnect", "school_bb_prepare_assignment_files", "school_bb_prepare_submission", "school_bb_submit_assignment", "school_bb_submission_status"}) for tool in tools.tools)
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
                assert names == {"school_jw_build_timetable", "school_jw_schedule_enrollment_watch", "school_jw_script_status", "school_jw_cancel_script", "school_jw_search_offerings", "school_jw_planning_context", "school_jw_plan_timetables", "school_jw_enrollment_window", "school_jw_prepare_course_change", "school_jw_status", "school_jw_reconnect", "school_jw_check_connection", "school_jw_read_home", "school_jw_list_modules", "school_jw_list_semesters", "school_jw_list_courses", "school_jw_timetable", "school_jw_grades"}, names
                status = await session.call_tool("school_jw_status", {})
                assert status.isError is False and status.structuredContent["configured"] is False
                assert status.structuredContent["credentials_saved"] is False
                missing = await session.call_tool("school_jw_list_courses", {})
                assert missing.isError is True
                assert all(tool.annotations.readOnlyHint is (tool.name not in {"school_jw_reconnect", "school_jw_schedule_enrollment_watch", "school_jw_cancel_script"}) for tool in tools.tools)
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


        for adapter in ("teach", "nan7", "icourse", "young", "finance"):
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
                    assert all(tool.annotations and tool.annotations.readOnlyHint is (tool.name not in {f"school_{adapter}_reconnect", "school_nan7_prepare_offer", "school_nan7_publish_offer", "school_icourse_prepare_review", "school_icourse_publish_review", "school_young_schedule_registration", "school_young_run_registration", "school_young_cancel_registration_task", "school_young_cancel_schedule"}) for tool in tools.tools)
                    assert all(tool.outputSchema is not None for tool in tools.tools)
                    status = await session.call_tool(status_name, {})
                    assert status.isError is False and isinstance(status.structuredContent, dict)
                    if adapter == "young":
                        assert names == {"school_young_status", "school_young_reconnect", "school_young_check_connection", "school_young_read_home", "school_young_find_projects", "school_young_schedule_registration", "school_young_run_registration", "school_young_registration_status", "school_young_cancel_registration_task", "school_young_schedule_status", "school_young_cancel_schedule"}
                        assert status.structuredContent["configured"] is False
                        missing = await session.call_tool("school_young_read_home", {})
                        assert missing.isError is True
                    if adapter == 'finance':
                        assert names == {'school_finance_entry_points', 'school_finance_status', 'school_finance_reconnect',
                                         'school_finance_check_connection', 'school_finance_list_services', 'school_finance_inspect_smart', 'school_finance_workflow_guide'}
                        assert status.structuredContent['configured'] is False
                        entry = await session.call_tool('school_finance_entry_points', {})
                        assert not entry.isError and entry.structuredContent['network_checked'] is False
                        missing = await session.call_tool('school_finance_check_connection', {})
                        assert missing.isError
                    print(json.dumps({"server": initialized.serverInfo.name, "tools": sorted(names), "public_adapter_status": "handled"}, ensure_ascii=False))


asyncio.run(main())
