"""Actual stdio workflow contracts against isolated synthetic IMAP; no real mail writes."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    if os.name != 'nt':
        print('Skipped: real DPAPI workflow requires Windows')
        return
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / 'mail.json').write_text(json.dumps({'address': 'student@mail.ustc.edu.cn'}), encoding='utf-8')
        env = {**os.environ, 'SCHOOL_MCP_LOCAL_DIR': directory, 'SCHOOL_MAIL_PASSWORD': '', 'PYTHONUTF8': '1'}
        parameters = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name('mail_workflow_server.py'))], env=env)
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()

                async def call(name, **arguments):
                    response = await session.call_tool('school_mail_' + name, arguments)
                    assert not response.isError, response
                    return response.structuredContent

                ref = {'mailbox': 'INBOX', 'uid': 1, 'uid_validity': 100}
                forward = await call('prepare_forward', message=ref, to=['recipient@example.com'], note='合成转发')
                assert forward['status'] == 'ready'
                reply = await call('prepare_reply_all', message=ref, body='合成回复')
                assert reply['preview']['to'] == ['office@example.edu']
                exported = await call('export', messages=[ref])
                assert Path(exported['path']).is_file()
                thread = await call('read_thread', message=ref)
                assert len(thread['messages']) == 1
                plan = await call('prepare_actions', items=[{**ref, 'action': 'mark_read'}])
                done = await call('execute_actions', plan_id=plan['plan_id'], plan_sha256=plan['plan_sha256'])
                assert done['status'] == 'completed'
                undo = await call('prepare_undo', plan_id=plan['plan_id'])
                assert (await call('execute_actions', plan_id=undo['plan_id'], plan_sha256=undo['plan_sha256']))['status'] == 'completed'
                await call('save_classification_rules', categories=['课程'], rules=[{'category': '课程', 'field': 'domain', 'value': 'example.edu'}])
                assert (await call('get_classification_rules'))['configured']
                preview = await call('preview_classification', messages=[ref])
                assert preview['items'][0]['decision'] == 'rule'
                plan = await call('prepare_classification', preview_id=preview['preview_id'], preview_sha256=preview['preview_sha256'])
                done = await call('execute_actions', plan_id=plan['plan_id'], plan_sha256=plan['plan_sha256'])
                assert done['status'] == 'completed'
                check = await call('action_status', plan_id=plan['plan_id'], reconcile=True)
                assert check['observations'][0]['after']['matches_snapshot']
                again = await call('execute_actions', plan_id=plan['plan_id'], plan_sha256=plan['plan_sha256'])
                assert again['items'][0]['destination'] == done['items'][0]['destination']
                assert (await call('create_folder', archive=True, categories=['活动']))['status'] == 'created'
                malformed = await session.call_tool('school_mail_prepare_actions', {'items': [{'mailbox': 'INBOX', 'uid': True, 'uid_validity': 100, 'action': 'archive'}]})
                assert malformed.isError
                print(json.dumps({'synthetic_mail_workflows': 'passed', 'protocol': 'stdio',
                                  'encryption': 'Windows DPAPI', 'real_mailbox_mutations': False,
                                  'SMTP_attempted': False}))


if __name__ == '__main__': asyncio.run(main())
