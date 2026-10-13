"""Desktop boundary/continuation tests; synthetic accounts and zero school writes."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from typing import Any
from unittest.mock import Mock, patch

import httpx
from mcp.server.fastmcp import FastMCP

from school_mcp.desktop.app import Application
from school_mcp.desktop.server import Server
from school_mcp.desktop.storage import Data, read_private, write_private
from school_mcp.desktop.model import ModelClient, ModelSettings, public_settings, save_settings
from school_mcp.desktop.chat import ChatRunner, skill_text
from school_mcp.bb.identity import load_credentials, save_session_identity, status
from school_mcp.bb.login import try_submit_credentials, session_only_step
from school_mcp.bb.session import BBError
from school_mcp.agent import engine
from school_mcp.agent.store import Store

SETTINGS = dict(base_url='https://model.example/v1', model='synthetic', api_key='synthetic-model-secret', tested=True)


def tool(name, arguments, identifier='call_synthetic'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': identifier, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


@unittest.skipUnless(os.name == 'nt', 'DPAPI is Windows user-bound')
class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        self.app = Application(self.tmp.name)
        self.data = self.app.data

    def configured(self):
        save_session_identity('synthetic-account')
        write_private(self.data.settings_path, SETTINGS)

    def test_encrypted_settings_no_key_in_status_or_chat_database(self):
        save_settings(self.data, {k: SETTINGS[k] for k in ModelSettings.model_fields})
        self.assertNotIn(SETTINGS['api_key'], json.dumps(public_settings(self.data)))
        self.assertNotIn(SETTINGS['api_key'].encode(), self.data.settings_path.read_bytes())
        chat = self.data.new_chat()
        chat['messages'] = [{'role': 'user', 'content': 'synthetic private question'}]
        self.data.save_chat(chat)
        self.assertNotIn(b'synthetic private question', (self.data.profile / 'desktop-chats.sqlite3').read_bytes())
        self.assertEqual(self.data.chat(chat['id'])['messages'], chat['messages'])
        with self.assertRaises(ValueError):
            save_settings(self.data, dict(base_url='https://another.example/v1', model='test', api_key=''))
        self.assertEqual(read_private(self.data.settings_path)['api_key'], SETTINGS['api_key'])

    def test_session_only_identity_does_not_fill_password_and_rejects_account_change(self):
        save_session_identity('synthetic-account')
        credentials = load_credentials()
        self.assertEqual(credentials['password'], '')
        self.assertTrue(credentials['session_only'])
        self.assertFalse(status()['credentials_saved'])
        self.assertTrue(status()['identity_saved'])
        page = Mock(url='https://id.ustc.edu.cn/cas/login')
        self.assertFalse(try_submit_credentials(page, credentials))
        page.locator.assert_not_called()
        with self.assertRaises(BBError):
            save_session_identity('another-account')
        self.assertEqual(load_credentials()['username'], 'synthetic-account')

    def test_expired_session_requests_manual_login_and_email_requires_opt_in(self):
        page = Mock(url='https://id.ustc.edu.cn/cas/login')
        page.locator.return_value.inner_text.return_value = '登录'
        page.locator.return_value.count.return_value = 1
        verification, notify = Mock(last_stage=None), Mock()
        session_only_step(page, {'session_only': True}, verification, notify)
        verification.step.assert_called_once_with(page)
        self.assertEqual(notify.call_args.args[0], 'waiting_for_verification')

    def test_data_root_guard_recovery_and_clear_scope(self):
        other = Path(self.tmp.name) / 'unrelated'
        other.mkdir()
        (other / 'keep.txt').write_text('unrelated', encoding='utf-8')
        with self.assertRaises(ValueError):
            Data(other)
        self.configured()
        chat = self.data.new_chat()
        chat['state'] = 'running'
        chat['pending'] = None
        self.data.save_chat(chat)
        self.data.recover()
        self.assertEqual(self.data.chat(chat['id'])['state'], 'interrupted')
        with patch.object(self.app, 'background_active', return_value=True):
            with self.assertRaises(ValueError):
                self.app.clear('all', 'all')
        with patch.object(self.app, 'background_active', return_value=False):
            self.app.clear('account', 'account')
        self.assertTrue(self.data.settings_path.exists())
        self.assertFalse(self.app.identity()['saved'])
        self.assertEqual(self.data.chats(), [])
        self.assertEqual((other / 'keep.txt').read_text(), 'unrelated')

    def test_logout_blocks_chat_and_upload_rejects_path_escape(self):
        self.configured()
        for name in ('../escape.txt', 'x:secret.txt', 'a/b.txt'):
            with self.assertRaises(ValueError):
                self.app.upload(name, b'synthetic')
        uploaded = self.app.upload('material.txt', b'synthetic')
        self.assertTrue(Path(uploaded['path']).is_relative_to(self.data.profile))
        with patch.object(self.app, 'background_active', return_value=False):
            self.app.clear('logout', 'logout')
        with self.assertRaises(ValueError):
            self.app.send({'text': 'hello'})
        with self.assertRaises(ValueError):
            self.app.upload('private.txt', b'synthetic')
        self.assertIsNone(self.app.status()['identity']['account'])

    def test_http_requires_token_host_origin_and_login(self):
        with Server(self.app) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with httpx.Client(base_url=server.origin, trust_env=False) as client:
                    self.assertEqual(client.get('/').status_code, 200)
                    self.assertEqual(client.get('/api/status').status_code, 403)
                    headers = {'X-School-Token': server.token, 'Origin': server.origin}
                    self.assertEqual(client.get('/api/status', headers=headers).status_code, 200)
                    self.assertEqual(client.get('/api/chats', headers=headers).status_code, 400)
                    self.assertEqual(client.get('/', headers={'Host': 'attacker.example'}).status_code, 403)
                    self.assertEqual(client.post('/api/stop', json={}, headers={**headers, 'Origin': 'https://attacker.example'}).status_code, 403)
                    self.assertEqual(client.post('/api/stop', json={}, headers={**headers, 'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
                    self.assertEqual(client.post('/api/stop', json={}, headers=headers).status_code, 200)
                    response = client.post('/api/model/save', json={**SETTINGS, 'unexpected': 'secret'}, headers=headers)
                    self.assertEqual(response.status_code, 400)
                    self.assertNotIn('secret', response.text)
                    self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
                    self.assertEqual(client.get('/../model-settings.dpapi', headers=headers).status_code, 400)
            finally:
                server.shutdown()
                thread.join(5)

    def test_model_disallows_insecure_remote_url_and_does_not_follow_redirect(self):
        for url in ('http://model.example/v1', 'https://u:p@model.example', 'https://model.example/?key=x'):
            with self.assertRaises(ValueError):
                ModelSettings.model_validate({k: url if k == 'base_url' else SETTINGS[k] for k in ModelSettings.model_fields})
        requests = []
        def redirect(request):
            requests.append(request)
            return httpx.Response(302, headers={'Location': 'https://attacker.example/'})
        with self.assertRaisesRegex(ValueError, '302'):
            ModelClient(SETTINGS, httpx.MockTransport(redirect)).complete([{'role': 'user', 'content': 'ping'}], [])
        self.assertEqual(len(requests), 1)
        self.assertNotIn(SETTINGS['api_key'].encode(), requests[0].content)

    def test_model_requires_actual_function_call_for_connection_test(self):
        self.configured()
        from school_mcp.desktop.model import test_connection
        with patch.object(ModelClient, 'complete', return_value={'role': 'assistant', 'content': 'ok'}):
            with self.assertRaises(ValueError):
                test_connection(self.data)
        def complete(messages, *_):
            if messages[-1]['role']=='tool':
                return {'role':'assistant','content':json.loads(messages[-1]['content'])['verification_code']}
            return tool('connection_check', {'ok':True})
        with patch.object(ModelClient, 'complete', side_effect=complete):
            self.assertTrue(test_connection(self.data)['tool_calling'])

    def run_chat(self, responses, *, permitted=False, task=None):
        self.configured()
        chat = self.data.new_chat()
        if task:
            chat['tasks'] = [task]
            self.data.save_chat(chat)
        with patch.object(ModelClient, 'complete', side_effect=responses):
            ChatRunner(self.data, threading.Event()).run(chat['id'], 'synthetic request', permitted)
        return self.data.chat(chat['id'])

    def test_real_coordinator_finance_flow_and_bundled_workflow_instructions(self):
        self.configured()
        task = Store().create('finance', 'synthetic preflight', None)['task_id']
        responses = [tool('school_agent_tools', {'task_id': task, 'tool_name': 'school_finance_preflight'}),
                     tool('school_agent_call', {'task_id': task, 'tool_name': 'school_finance_preflight',
                          'arguments': {'request': {'business_type': 'travel'}}}, 'call_preflight'),
                     {'role': 'assistant', 'content': '请补充差旅材料。'}]
        chat = self.run_chat(responses, task=task)
        self.assertEqual(chat['state'], 'idle')
        results = [json.loads(m['content']) for m in chat['messages'] if m['role'] == 'tool']
        self.assertIn('workflow_instructions', results[0])
        self.assertFalse(results[1]['result']['safe_to_submit'])
        self.assertIn('input_guidance', results[1]['result'])
        self.assertGreater(len(skill_text('finance')), 100)

    def test_human_approval_overrides_model_flag_and_survives_restart_without_replay(self):
        self.configured()
        task = Store().create('bb', 'synthetic submission', None)['task_id']
        fake = FastMCP('synthetic-desktop')
        calls = []
        @fake.tool()
        def school_bb_submit_assignment(preparation_id: str, expected_sha256: str) -> dict[str, Any]:
            calls.append(preparation_id)
            return {'state': 'verified', 'preparation_id': preparation_id}
        args = {'task_id': task, 'tool_name': 'school_bb_submit_assignment', 'authorized': True,
                'arguments': {'preparation_id': 'a'*32, 'expected_sha256': 'b'*64}}
        with patch.object(engine, 'adapter', return_value=fake):
            chat = self.run_chat([tool('school_agent_call', args)], task=task)
            self.assertEqual(chat['state'], 'waiting_for_approval')
            self.assertEqual(calls, [])
            self.data.recover()
            pending = self.data.chat(chat['id'])['pending']['id']
            runner = ChatRunner(self.data, threading.Event())
            with patch.object(ModelClient, 'complete', return_value={'role': 'assistant', 'content': '已核实。'}):
                runner.run(chat['id'], approval=pending)
            self.assertEqual(len(calls), 1)
            with self.assertRaises(ValueError):
                runner.run(chat['id'], approval=pending)
            self.assertEqual(len(calls), 1)
            self.assertIsNone(self.data.chat(chat['id'])['pending'])

    def test_file_whitelist_and_task_ownership(self):
        self.configured()
        runner = ChatRunner(self.data, threading.Event())
        chat = self.data.new_chat()
        with self.assertRaises(ValueError):
            runner.file_check(chat, {'attachments': [str(self.data.settings_path)]})
        other = Store().create('finance', 'another conversation', None)['task_id']
        with self.assertRaises(ValueError):
            asyncio.run(runner.dispatch(chat, 'school_agent_task', {'task_id': other}))

    def test_interrupted_turn_closes_tool_calls_without_replay(self):
        self.configured()
        chat = self.data.new_chat()
        chat['messages'] = [tool('school_agent_call', {'tool_name': 'school_bb_submit_assignment'})]
        chat['state'] = 'interrupted'
        self.data.save_chat(chat)
        with patch.object(ModelClient, 'complete', return_value={'role': 'assistant', 'content': '先核实原记录。'}), patch.object(ChatRunner, 'dispatch') as dispatch:
            ChatRunner(self.data, threading.Event()).run(chat['id'], '继续')
        dispatch.assert_not_called()
        saved = self.data.chat(chat['id'])
        self.assertEqual(sum(m['role'] == 'tool' for m in saved['messages']), 1)
        self.assertIn('interrupted', saved['messages'][1]['content'])

    def test_packaged_script_launcher_uses_exe_not_pythonw(self):
        from school_mcp.script_jobs import JobStore, launch, now
        from datetime import timedelta
        store = JobStore()
        job = store.create('jw', 'jw_watch', {}, now().isoformat(), (now()+timedelta(minutes=1)).isoformat(), 60, None)
        with patch('sys.frozen', True, create=True), patch('sys.executable', 'C:/synthetic/USTC-School-Agent.exe'), \
             patch('school_mcp.background_process.spawn', return_value=Mock(pid=123)) as spawn, \
             patch('school_mcp.login_runtime.process_started_at', return_value=1):
            launch(job, store)
        self.assertEqual(Path(spawn.call_args.args[0][0]).name, 'USTC-School-Agent.exe')
        self.assertEqual(Path(spawn.call_args.kwargs['cwd']).name, 'synthetic')

    def test_unbind_removes_pending_secret_but_keeps_creation_guard(self):
        self.configured()
        journal = self.data.profile / 'mail-browser-setup.dpapi'
        write_private(journal, {'state': 'captured', 'password': 'pending-synthetic-secret', 'label': 'synthetic'})
        self.app.clear('mail', 'mail')
        value = read_private(journal)
        self.assertEqual(value['state'], 'creation_attempted')
        self.assertNotIn('password', value)


if __name__ == '__main__':
    unittest.main()
