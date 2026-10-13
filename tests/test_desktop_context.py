"""Deterministic context/recovery acceptance with actual MCP wrappers, no school I/O."""
import asyncio
import copy
import json
import os
import tempfile
import threading
import unittest
from typing import Any
from unittest.mock import patch

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from school_mcp.recovery import failure
from school_mcp.service_errors import ServiceError
from school_mcp.desktop.context import project, units
from school_mcp.desktop.model import ModelClient, ModelError, request_payload
from school_mcp.desktop.chat import ChatRunner
from school_mcp.desktop.app import Application
from school_mcp.desktop.storage import write_private
from school_mcp.agent import engine
from school_mcp.agent.store import Store
from school_mcp.workflow_store import digest
from school_mcp.bb.identity import save_session_identity


CONFIG = dict(base_url='https://model.example/v1', model='synthetic', api_key='synthetic-key', tested=True)
FINAL = {'role': 'assistant', 'content': '本轮已停止，请检查回执。'}


def call(name, args, identifier='call_test'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': identifier, 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(args)}}]}


class ClassificationTests(unittest.TestCase):
    def test_typed_causes_not_exception_text(self):
        private = 'https://private.example/?token=synthetic-secret'
        for code in ('authentication_required', 'access_denied', 'cooldown', 'runtime_failure'):
            wrapped = ToolError(private)
            wrapped.__cause__ = ServiceError(private, code=code, retry_after_seconds=17)
            result = failure(wrapped)
            self.assertEqual(result['error_code'], code)
            self.assertNotIn(private, json.dumps(result))
            self.assertFalse(result['recovery']['automatic_retry'])
        self.assertEqual(failure(RuntimeError('authentication_required ' + private))['error_code'], 'unavailable')

    def test_http_errors_and_unknown_write_never_offer_replay(self):
        request = httpx.Request('GET', 'https://example.invalid')
        for status, code in ((401, 'authentication_required'), (403, 'access_denied'), (429, 'cooldown'), (503, 'service_unavailable')):
            error = httpx.HTTPStatusError('private body', request=request, response=httpx.Response(status))
            result = failure(error, write_may_have_run=True)
            self.assertEqual(result['error_code'], code)
            self.assertEqual(result['state'], 'needs_verification')
            self.assertEqual(result['recovery']['action'], 'verify_original_receipt')
            self.assertFalse(result['recovery']['retry_write_allowed'])
        self.assertEqual(failure(httpx.ReadTimeout('private'))['error_code'], 'timeout')


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
class ContextTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        self.app = Application(tmp.name)
        self.data = self.app.data
        save_session_identity('synthetic-account')
        write_private(self.data.settings_path, CONFIG)
        self.chat = self.data.new_chat()
        self.runner = ChatRunner(self.data, threading.Event())

    def old_queries(self, count=8):
        for i in range(count):
            self.chat['messages'] += [
                {'role': 'user', 'content': f'第 {i} 轮的精确要求，不允许改动目标 {i}。'},
                call('school_agent_route', {'goal': 'synthetic'}, 'c'+str(i)),
                {'role': 'tool', 'tool_call_id': 'c'+str(i), 'content': json.dumps({'body': '旧查询' * 15000}, ensure_ascii=False)},
                {'role': 'assistant', 'content': '旧结果说明'}]
        self.data.save_chat(self.chat)

    def task(self):
        task = Store().create('bb', '原目标不能改写', None)
        self.chat['tasks'] = [task['task_id']]
        self.data.save_chat(self.chat)
        return task

    def test_projection_preserves_all_user_corrections_pairs_and_original_history(self):
        self.old_queries()
        self.chat['messages'].append({'role': 'user', 'content': '最终更正：只准备，不提交。'})
        original = copy.deepcopy(self.chat)
        projected, report = project(self.chat, 'system', [], 'synthetic', budget=10000)
        self.assertTrue(report['compacted'])
        self.assertLessEqual(len(request_payload('synthetic', projected, [])), 10000)
        self.assertEqual([m for m in projected if m['role'] == 'user'],
                         [m for m in original['messages'] if m['role'] == 'user'])
        units(projected)
        self.assertEqual(self.chat, original)
        self.assertNotIn('旧查询', json.dumps(projected, ensure_ascii=False))

    def test_exact_write_parameters_receipts_files_and_pending_survive_projection(self):
        task = self.task()
        arguments = {'preparation_id': 'a'*32, 'expected_sha256': 'b'*64, 'files': ['synthetic.pdf']}
        task['calls'] = [{'tool': 'school_bb_submit_assignment', 'mode': 'execute', 'arguments': arguments,
                          'key': digest(['school_bb_submit_assignment', arguments]),
                          'status': 'failed_or_unknown', 'authorized': True,
                          'receipt': {'state': 'uncertain', 'preparation_id': 'a'*32}}]
        task['write_blocked'] = True
        task['links'] = [{'tool': 'school_bb_submission_status', 'arguments': {'preparation_id': 'a'*32}}]
        store = Store()
        with store.db() as db:
            store.save(db, task)
        self.chat['allowed_files'] = ['synthetic.pdf']
        self.chat['pending'] = {'id': 'exact-pending', 'arguments': arguments}
        self.old_queries()
        projected, _ = project(self.chat, 'system', [], 'synthetic', budget=10000)
        memory = json.loads(projected[1]['content'])
        self.assertEqual(memory['tasks'][0]['operations'][0]['arguments'], arguments)
        self.assertEqual(memory['tasks'][0]['operations'][0]['receipt']['state'], 'uncertain')
        self.assertEqual(memory['tasks'][0]['goal'], task['goal'])
        self.assertTrue(memory['tasks'][0]['write_blocked'])
        self.assertEqual(memory['pending'], self.chat['pending'])
        self.assertEqual(memory['allowed_files'], ['synthetic.pdf'])

    def test_budget_never_discards_user_constraints_or_queries_model_with_partial_task(self):
        self.chat['messages'] = [{'role': 'user', 'content': '用户条件' * 10000}]
        with self.assertRaises(ModelError) as error:
            project(self.chat, 'system', [], 'synthetic', budget=1000)
        self.assertEqual(error.exception.code, 'context_capacity')
        self.assertEqual(len(self.chat['messages'][0]['content']), 40000)

    def test_small_context_is_unchanged_and_retains_reasoning(self):
        self.chat['messages'] = [{'role': 'user', 'content': '查通知'},
                                 dict(FINAL, reasoning_content='synthetic thinking')]
        result, report = project(self.chat, 'system', [], 'synthetic')
        self.assertFalse(report['compacted'])
        self.assertEqual(result[1:], self.chat['messages'])

    def test_broken_batches_and_inaccessible_tasks_stop_without_guessing(self):
        self.old_queries()
        self.chat['messages'].append(call('school_agent_route', {}))
        with self.assertRaises(ModelError) as error:
            project(self.chat, 'system', [], 'synthetic', budget=10000)
        self.assertEqual(error.exception.code, 'context_incomplete_batch')
        self.chat['messages'].pop()
        self.chat['tasks'] = ['f'*32]
        with self.assertRaises(ModelError) as error:
            project(self.chat, 'system', [], 'synthetic', budget=10000)
        self.assertEqual(error.exception.code, 'context_task_unavailable')

    def test_actual_wrapped_service_errors_classify_connection_without_relogin(self):
        for code, connection_state in (('authentication_required', 'login_required'), ('access_denied', 'unavailable'),
                                       ('cooldown', 'unavailable'), ('timeout', 'unavailable')):
            fake = FastMCP('synthetic-service')
            @fake.tool()
            def school_bb_check_connection() -> dict[str, Any]:
                raise ServiceError('synthetic-private-detail', code=code, retry_after_seconds=9)
            with patch('school_mcp.desktop.connections.adapter', return_value=fake):
                self.app.check_service('bb')
                self.app.worker.join(5)
            self.assertEqual(self.app.connections['bb']['state'], connection_state)
            self.assertEqual(self.app.operation['result']['error_code'], code)
            self.assertEqual(self.app.operation['state'], 'attention')
            self.assertNotIn('synthetic-private-detail', json.dumps(self.app.operation))

    def test_coordinator_read_failure_reaches_chat_and_stops_the_loop(self):
        task = self.task()
        fake = FastMCP('synthetic-service')
        @fake.tool()
        def school_bb_list_courses() -> dict[str, Any]:
            raise ServiceError('synthetic-private-detail', code='cooldown', retry_after_seconds=23)
        response = call('school_agent_call', {'task_id': task['task_id'], 'tool_name': 'school_bb_list_courses', 'arguments': {}})
        with patch.object(engine, 'adapter', return_value=fake), patch.object(ModelClient, 'complete', return_value=response) as model:
            result = self.runner.run(self.chat['id'], '合成查询')
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(result['error_code'], 'cooldown')
        stored = self.data.chat(self.chat['id'])
        self.assertEqual(stored['last_failure']['retry_after_seconds'], 23)
        self.assertFalse(Store().read(task['task_id']).get('write_blocked'))

    def test_unknown_write_retains_reason_and_block_after_restart(self):
        task = self.task()
        fake = FastMCP('synthetic-service')
        @fake.tool()
        def school_bb_submit_assignment(preparation_id: str, expected_sha256: str) -> dict[str, Any]:
            raise ServiceError('synthetic-private-detail', code='timeout')
        args = {'task_id': task['task_id'], 'tool_name': 'school_bb_submit_assignment',
                'arguments': {'preparation_id': 'a'*32, 'expected_sha256': 'b'*64}}
        with patch.object(engine, 'adapter', return_value=fake), patch.object(ModelClient, 'complete', return_value=call('school_agent_call', args)):
            result = self.runner.run(self.chat['id'], '合成提交', True)
        self.assertEqual(result['recovery']['action'], 'verify_original_receipt')
        self.assertEqual(result['error_code'], 'timeout')
        stored = Store().read(task['task_id'])
        self.assertTrue(stored['write_blocked'])
        self.assertEqual(stored['calls'][0]['receipt']['error_code'], 'timeout')
        replay = asyncio.run(engine.call(task['task_id'], args['tool_name'], args['arguments'], True))
        self.assertFalse(replay['dispatched'])

    def test_compacted_chat_can_continue_but_cannot_skip_restore_before_write(self):
        task = self.task()
        self.old_queries()
        args = {'task_id': task['task_id'], 'tool_name': 'school_bb_submit_assignment',
                'arguments': {'preparation_id': 'a'*32, 'expected_sha256': 'b'*64}}
        with patch.object(ModelClient, 'complete', side_effect=[call('school_agent_call', args), FINAL]) as model:
            result = self.runner.run(self.chat['id'], '先准备')
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(Store().read(task['task_id'])['calls'], [])
        transcript = self.data.chat(self.chat['id'])
        self.assertTrue(transcript['context_projection']['compacted'])
        last_tool = next(m for m in reversed(transcript['messages']) if m['role'] == 'tool')
        self.assertEqual(json.loads(last_tool['content'])['state'], 'needs_context')
        self.assertLess(len(request_payload('synthetic', *model.call_args.args)), 192001)
        # A new runner simulates continuing after restart. Restoring is a read, not authorization.
        runner = ChatRunner(self.data, threading.Event())
        restore = call('school_agent_task', {'task_id': task['task_id'], 'include_context': True})
        with patch.object(ModelClient, 'complete', side_effect=[restore, call('school_agent_call', args)]):
            result = runner.run(self.chat['id'], '恢复后准备给我确认')
        self.assertEqual(result['state'], 'waiting_for_approval')
        self.assertEqual(Store().read(task['task_id'])['calls'], [])


if __name__ == '__main__':
    unittest.main()
