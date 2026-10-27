"""Failure-path regression coverage for the desktop executor; no school network."""
import copy
import json
import os
import tempfile
import threading
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from mcp.server.fastmcp import FastMCP
from school_mcp.desktop.app import Application
from school_mcp.desktop.chat import ChatRunner
from school_mcp.desktop.model import ModelClient, ModelError, test_connection, validated_message, tool_arguments
from school_mcp.desktop.storage import read_private, write_private
from school_mcp.bb.identity import save_session_identity
from school_mcp.agent import engine
from school_mcp.agent.store import Store


CONFIG = {'base_url': 'https://model.example/v1', 'model': 'synthetic', 'api_key': 'synthetic-private-key', 'tested': True}
FINAL = {'role': 'assistant', 'content': '处理已结束，请检查原记录。'}


def call(name='school_agent_route', args=None, identifier='call_one'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': identifier, 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(args or {'goal': '合成测试'})}}]}


class ResponseTests(unittest.TestCase):
    def test_response_and_argument_validation_before_dispatch(self):
        bad_calls = [None, 1, {}, 'function', [{'id': 'x'}]]
        for value in bad_calls[1:]:
            with self.subTest(value=value), self.assertRaises(ModelError):
                validated_message({'role': 'assistant', 'content': '说明', 'tool_calls': value})
        invalid = [call(), call(), call(), call()]
        invalid[0]['tool_calls'][0]['function']['arguments'] = '[]'
        invalid[1]['tool_calls'][0]['function']['arguments'] = '{"authorized":false,"authorized":true}'
        invalid[2]['tool_calls'][0]['function']['arguments'] = '{"n":NaN}'
        invalid[3]['tool_calls'] *= 2
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ModelError):
                validated_message(value)
        for value in ('{"n":1e309}', '{"n":Infinity}', '[1]', 'null', '{bad'):
            with self.assertRaises(ModelError):
                tool_arguments(value)
        self.assertEqual(tool_arguments('{"count":1,"ratio":1.5}'), {'count': 1, 'ratio': 1.5})

    def test_incomplete_refused_or_empty_response_never_yields_a_tool(self):
        for finish in ('length', 'content_filter'):
            client = ModelClient(CONFIG, httpx.MockTransport(lambda req: httpx.Response(200, json={'choices': [
                {'finish_reason': finish, 'message': call()}]})))
            with self.assertRaises(ModelError) as error:
                client.complete([], [])
            self.assertEqual(error.exception.code, 'incomplete_model_response')
        for message in ({'role': 'assistant', 'content': None},
                        {'role': 'assistant', 'content': ['invalid']},
                        {'role': 'assistant', 'content': None, 'refusal': 'synthetic private reason'}):
            with self.assertRaises(ModelError) as error:
                validated_message(message)
            self.assertNotIn('synthetic private', str(error.exception))

    def test_http_failure_is_coded_and_never_retries_or_exposes_body(self):
        requests = []
        def response(request):
            requests.append(request)
            return httpx.Response(429, text='synthetic private server response')
        with self.assertRaises(ModelError) as error:
            ModelClient(CONFIG, httpx.MockTransport(response)).complete([], [])
        self.assertEqual(error.exception.code, 'model_rate_limited')
        self.assertEqual(len(requests), 1)
        self.assertNotIn('synthetic private', str(error.exception))


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        self.app = Application(self.tmp.name)
        self.data = self.app.data
        save_session_identity('synthetic-account')
        write_private(self.data.settings_path, CONFIG)
        self.stop = threading.Event()
        self.runner = ChatRunner(self.data, self.stop)
        self.chat = self.data.new_chat()

    def test_stop_feedback_stays_pending_until_original_worker_finishes(self):
        entered, release = threading.Event(), threading.Event()
        def waiting():
            entered.set()
            if not release.wait(5):
                raise AssertionError('Test release missing')
            return {'state': 'stopped', 'note': '测试请求结束'}
        self.app.start('synthetic_wait', waiting)
        try:
            self.assertTrue(entered.wait(2))
            self.app.api('/api/stop', {})
            self.app.notify('late_callback', '请求尚未结束')
            self.assertTrue(self.app.busy())
            saved = read_private(self.data.root / 'operation.dpapi')
            self.assertEqual(saved['state'], 'running')
            self.assertEqual(saved['stage'], 'stopping')
            self.assertTrue(saved['stop_requested'])
            self.assertIn('核实', saved['detail'])
            with self.assertRaises(ValueError):
                self.app.start('duplicate', lambda: {})
        finally:
            release.set()
            self.app.worker.join(5)
        self.assertEqual(self.app.operation['state'], 'stopped')
        self.app.start('next_operation', lambda: {})
        self.app.worker.join(5)
        self.assertNotIn('stop_requested', self.app.operation)

    def test_followup_preserves_searchable_conversation_title(self):
        self.chat['messages'] = [{'role': 'assistant', 'content': '欢迎使用'}]
        self.data.save_chat(self.chat)
        with patch.object(ModelClient, 'complete', return_value=FINAL):
            self.runner.run(self.chat['id'], '查看本周课程和作业')
            saved = self.data.chat(self.chat['id'])
            self.assertEqual(saved['title'], '查看本周课程和作业')
            saved['title'] = '已有的课程安排记录'
            self.data.save_chat(saved)
            self.runner.run(self.chat['id'], '谢谢，再看一下下周的')
        saved = self.data.chat(self.chat['id'])
        self.assertEqual(saved['title'], '已有的课程安排记录')
        self.assertEqual(sum(m['role'] == 'user' for m in saved['messages']), 2)

    def test_stop_during_final_save_preserves_the_completed_receipt(self):
        saving, release = threading.Event(), threading.Event()
        caller = threading.current_thread()
        def save(path, value):
            if threading.current_thread() is not caller and value.get('state') == 'completed':
                saving.set()
                if not release.wait(5):
                    raise AssertionError('Test release missing')
            write_private(path, value)
        with patch('school_mcp.desktop.app.write_private', side_effect=save):
            self.app.start('synthetic_final_save', lambda: {'note': '原请求已完成，回执已核实'})
            try:
                self.assertTrue(saving.wait(2))
                self.assertTrue(self.app.busy())
                self.app.api('/api/stop', {})
                self.assertEqual(self.app.operation['state'], 'completed')
                self.assertEqual(self.app.operation['detail'], '原请求已完成，回执已核实')
                self.assertNotIn('stop_requested', self.app.operation)
            finally:
                release.set()
                self.app.worker.join(5)
        saved = read_private(self.data.root / 'operation.dpapi')
        self.assertEqual(saved['state'], 'completed')
        self.assertEqual(saved['detail'], '原请求已完成，回执已核实')

    def test_unknown_task_snapshot_can_be_read_without_stopping_recovery(self):
        task=Store().create('vlab','synthetic recovery',None)
        task.update(state='needs_verification',write_blocked=True,
                    links=[{'tool':'school_vlab_status','arguments':{'plan_id':'a'*32}}])
        with Store().db() as db:Store().save(db,task)
        self.chat['tasks'].append(task['task_id']);self.data.save_chat(self.chat)
        with patch.object(ModelClient,'complete',side_effect=[call('school_agent_task',{'task_id':task['task_id'],'include_context':True}),FINAL]):
            self.runner.run(self.chat['id'],'核实原任务状态')
        result=self.data.chat(self.chat['id'])
        self.assertEqual(result['state'],'idle')
        self.assertNotIn('last_failure',result)
        self.assertTrue(Store().read(task['task_id'])['write_blocked'])

    def submission(self):
        self.task = Store().create('bb', 'synthetic test only', None)['task_id']
        self.chat['tasks'] = [self.task]
        self.data.save_chat(self.chat)
        return {'task_id': self.task, 'tool_name': 'school_bb_submit_assignment',
                'arguments': {'preparation_id': 'a'*32, 'expected_sha256': 'b'*64}, 'authorized': True}

    def test_stop_while_waiting_for_model_does_not_execute_late_tool(self):
        args = self.submission()
        entered, release = threading.Event(), threading.Event()
        result, failures = [], []
        def complete(*_):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Test release missing')
            return call('school_agent_call', args)
        def work():
            try:
                result.append(self.runner.run(self.chat['id'], '合成提交', True))
            except Exception as error:
                failures.append(error)
        with patch.object(ModelClient, 'complete', side_effect=complete), patch.object(self.runner, 'dispatch', new_callable=AsyncMock) as dispatch:
            thread = threading.Thread(target=work)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.stop.set()
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            dispatch.assert_not_called()
        self.assertEqual(failures, [])
        self.assertEqual(result[0]['state'], 'interrupted')
        self.assertFalse(any('tool_calls' in m for m in self.data.chat(self.chat['id'])['messages']))
        self.assertEqual(Store().read(self.task)['calls'], [])

    def test_stop_after_tool_preserves_receipt_without_next_model_request(self):
        self.submission()
        fake = FastMCP('synthetic-desktop-stop')
        @fake.tool()
        def school_bb_list_courses() -> dict[str, Any]:
            self.stop.set()
            return {'courses': []}
        with patch.object(engine, 'adapter', return_value=fake), patch.object(ModelClient, 'complete', return_value=call(
                'school_agent_call', {'task_id': self.task, 'tool_name': 'school_bb_list_courses', 'arguments': {}})) as complete:
            result = self.runner.run(self.chat['id'], '合成查询')
        self.assertEqual(complete.call_count, 1)
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(Store().read(self.task)['calls'][0]['status'], 'returned')
        self.assertEqual(self.data.chat(self.chat['id'])['messages'][-1]['role'], 'tool')

    def test_malformed_message_is_not_persisted_and_next_turn_can_continue(self):
        malformed = call()
        malformed['tool_calls'][0]['function']['arguments'] = '{invalid'
        with patch.object(ModelClient, 'complete', return_value=malformed):
            with self.assertRaises(ModelError):
                self.runner.run(self.chat['id'], '第一轮')
        stored = self.data.chat(self.chat['id'])
        self.assertEqual(len(stored['messages']), 1)
        self.assertEqual(stored['last_error_code'], 'invalid_tool_arguments')
        with patch.object(ModelClient, 'complete', return_value=FINAL):
            result = self.runner.run(self.chat['id'], '继续')
        self.assertEqual(result['state'], 'completed')

    def test_multi_call_batch_records_unexecuted_siblings(self):
        response = call()
        response['tool_calls'] += call('school_agent_start', {'workflow': 'finance', 'goal': '不得自动执行'}, 'call_two')['tool_calls']
        with patch.object(ModelClient, 'complete', side_effect=[response, FINAL]) as complete:
            result = self.runner.run(self.chat['id'], '合成查询')
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(self.data.chat(self.chat['id'])['tasks'], [])
        second_request = complete.call_args_list[1].args[0]
        batch = next(m for m in second_request if m.get('tool_calls'))
        self.assertEqual(len(batch['tool_calls']), 2)
        replies = [m for m in second_request if m['role'] == 'tool']
        self.assertEqual({m['tool_call_id'] for m in replies}, {'call_one', 'call_two'})
        skipped = next(json.loads(m['content']) for m in replies if m['tool_call_id'] == 'call_two')
        self.assertFalse(skipped['dispatched'])
        self.assertEqual(skipped['state'], 'not_executed')

    def test_pending_batch_and_cancellation_close_every_call_once(self):
        args = self.submission()
        response = call('school_agent_call', args)
        response['tool_calls'] += call(identifier='call_two')['tool_calls']
        with patch.object(ModelClient, 'complete', return_value=response):
            result = self.runner.run(self.chat['id'], '先准备')
        self.assertEqual(result['state'], 'waiting_for_approval')
        saved = self.data.chat(self.chat['id'])
        pending_id = saved['pending']['id']
        self.app.approval(saved['id'], pending_id, False)
        saved = self.data.chat(saved['id'])
        self.assertEqual(len([m for m in saved['messages'] if m['role'] == 'tool']), 2)
        self.assertFalse(saved['turn_authorized'])
        with patch.object(ModelClient, 'complete', return_value=FINAL):
            self.runner.run(saved['id'], '只解释')
        self.assertEqual(Store().read(self.task)['calls'], [])

    def test_stop_before_approval_keeps_confirmation_and_does_not_submit(self):
        args = self.submission()
        with patch.object(ModelClient, 'complete', return_value=call('school_agent_call', args)):
            self.runner.run(self.chat['id'], '先准备')
        pending = self.data.chat(self.chat['id'])['pending']['id']
        self.stop.set()
        with patch.object(self.runner, 'dispatch', new_callable=AsyncMock) as dispatch:
            result = self.runner.run(self.chat['id'], approval=pending)
        dispatch.assert_not_called()
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(self.data.chat(self.chat['id'])['pending']['id'], pending)

    def test_recovery_pairs_reused_ids_per_batch_and_preserves_order(self):
        self.chat['messages'] = [call(), {'role': 'tool', 'tool_call_id': 'call_one', 'content': '{}'},
                                 {'role': 'user', 'content': '第二轮'}, call(), {'role': 'user', 'content': '中断后的新问题'}]
        self.runner.close_dangling(self.chat)
        messages = self.chat['messages']
        self.assertEqual([m['role'] for m in messages], ['assistant', 'tool', 'user', 'assistant', 'tool', 'user'])
        self.assertEqual(json.loads(messages[4]['content'])['state'], 'interrupted')
        once = copy.deepcopy(messages)
        self.runner.close_dangling(self.chat)
        self.assertEqual(self.chat['messages'], once)

    def test_operation_reports_interruption_approval_and_model_error_truthfully(self):
        for state in ('interrupted', 'stopped', 'waiting_for_approval'):
            self.app.start('chat', lambda: {'state': state, 'next_step': 'synthetic state'})
            self.app.worker.join(5)
            self.assertEqual(self.app.operation['state'], state)
        def failure():
            raise ModelError('model_timeout', '模型响应超时；不会自动重复请求。')
        self.app.start('chat', failure)
        self.app.worker.join(5)
        self.assertEqual(self.app.operation['state'], 'failed')
        self.assertEqual(self.app.operation['error_code'], 'model_timeout')

    def test_rejected_input_and_busy_request_do_not_create_empty_chats(self):
        before = len(self.data.chats())
        for value in ({'text': ''}, {'text': 'valid', 'files': ['missing.txt']},
                      {'text': 'valid', 'files': {}}, {'text': 'valid', 'chat_id': 42}):
            with self.assertRaises(ValueError):
                self.app.send(value)
            self.assertEqual(len(self.data.chats()), before)
        with patch.object(self.app, 'busy', return_value=True):
            with self.assertRaises(ValueError):
                self.app.send({'text': 'valid'})
        self.assertEqual(len(self.data.chats()), before)

    def test_failed_model_recheck_revokes_old_success_and_requires_boolean(self):
        with patch.object(ModelClient, 'complete', return_value=call('connection_check', {'ok': 1})):
            with self.assertRaises(ValueError):
                test_connection(self.data)
        self.assertFalse(read_private(self.data.settings_path)['tested'])
        self.assertEqual(read_private(self.data.settings_path)['api_key'], CONFIG['api_key'])

    def test_cross_chat_write_is_rejected_before_requesting_approval(self):
        args = self.submission()
        self.chat['tasks'] = []
        self.data.save_chat(self.chat)
        with patch.object(ModelClient, 'complete', return_value=call('school_agent_call', args)):
            result = self.runner.run(self.chat['id'], '合成操作')
        self.assertEqual(result['state'], 'interrupted')
        self.assertIsNone(self.data.chat(self.chat['id'])['pending'])
        self.assertEqual(Store().read(self.task)['calls'], [])

    def test_shutdown_blocks_new_work_without_interrupting_an_active_turn(self):
        with patch.object(self.app, 'busy', return_value=True):
            with self.assertRaises(ValueError):
                self.app.shutdown()
        self.assertFalse(self.app.closing)
        self.app.shutdown()
        before = len(self.data.chats())
        with self.assertRaises(ValueError):
            self.app.send({'text': '关闭期间到达的新请求'})
        with self.assertRaises(ValueError):
            self.app.start('query', lambda: {})
        self.assertEqual(len(self.data.chats()), before)


if __name__ == '__main__':
    unittest.main()
