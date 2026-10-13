"""Coordinator behavior with synthetic handlers; no school writes or credentials."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP
from school_mcp.agent import engine
from school_mcp.agent.catalog import WORKFLOWS, describe, route, entry_schema
from school_mcp.agent.store import Store
from school_mcp.agent.server import school_agent_start, school_agent_task, school_agent_tasks
from school_mcp.first_use import first_use


@unittest.skipUnless(os.name == 'nt', 'Task encryption uses Windows DPAPI')
class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'SCHOOL_MCP_LOCAL_DIR': self.tmp.name, 'SCHOOL_MAIL_PASSWORD': ''})
        env.start()
        self.addCleanup(env.stop)
        self.count = 0
        self.server = FastMCP('synthetic-school')

        @self.server.tool()
        async def school_bb_submit_assignment(preparation_id: str, expected_sha256: str) -> dict[str, Any]:
            self.count += 1
            return dict(state='verified', preparation_id=preparation_id)

        @self.server.tool()
        def school_bb_submission_status(preparation_id: str, verify: bool = False) -> dict[str, Any]:
            return dict(state='verified', preparation_id=preparation_id)

        @self.server.tool()
        def school_bb_prepare_submission(course_id: str, content_id: str,
                                          resubmit: bool = False, allow_late: bool = False) -> dict[str, Any]:
            return dict(state='ready', preparation_id='a'*32, content_sha256='b'*64)

        @self.server.tool()
        def school_bb_list_courses(keyword: str = '') -> dict[str, Any]:
            return dict(state='completed', courses=[{'body': 'synthetic private query response'}])

        self.task = Store().create('bb', 'synthetic private task goal', None)['task_id']
        self.arguments = dict(preparation_id='a'*32, expected_sha256='b'*64)

    async def dispatch(self, name='school_bb_submit_assignment', args=None, authorized=True):
        with patch.object(engine, 'adapter', return_value=self.server), patch.object(engine, 'identity', return_value=None):
            return await engine.call(self.task, name, self.arguments if args is None else args, authorized)

    def task_data(self):
        store = Store()
        with store.db() as db:
            return store.load(db, self.task)

    async def test_missing_authorization_never_dispatches_and_existing_authorization_works(self):
        blocked = await self.dispatch(authorized=False)
        self.assertEqual(blocked['state'], 'authorization_required')
        self.assertEqual(self.count, 0)
        result = await self.dispatch()
        self.assertEqual(result['result']['state'], 'verified')
        self.assertEqual(self.count, 1)
        self.assertEqual(self.task_data()['calls'][0]['arguments'], self.arguments)
        self.assertEqual(engine.view(self.task_data())['business_result_source'], 'original_tool_receipt')
        self.assertEqual(engine.view(self.task_data())['state'], 'verified')

    async def test_exact_write_replay_and_restart_are_blocked(self):
        await self.dispatch()
        result = await self.dispatch()
        self.assertEqual(result['state'], 'already_attempted')
        self.assertEqual(self.count, 1)
        self.assertEqual(result['links'][0]['arguments']['preparation_id'], 'a'*32)

    async def test_schema_missing_unknown_and_secret_arguments_never_dispatch(self):
        result = await self.dispatch(args={'preparation_id': 'a'*32})
        self.assertEqual(result['state'], 'needs_input')
        self.assertEqual(result['input_guidance']['fields'][0]['path'], '/arguments/expected_sha256')
        self.assertEqual(result['input_guidance']['resume']['tool'], 'school_agent_call')
        self.assertEqual(result['input_guidance']['fields'][0]['source'], 'tool_result')
        for extra in ({'made_up_argument': 1}, {'password': 'synthetic-secret'}):
            with self.assertRaises(ValueError):
                await self.dispatch(args=self.arguments | extra)
        self.assertEqual(self.count, 0)
        self.assertEqual(self.task_data()['calls'], [])

    async def test_cross_workflow_and_protected_flags(self):
        with self.assertRaises(ValueError):
            await self.dispatch('school_nan7_publish_offer', {})
        with self.assertRaises(ValueError):
            await self.dispatch('school_bb_prepare_submission', {'course_id': 'c', 'content_id': 'x', 'allow_late': True}, False)
        result = await self.dispatch('school_bb_prepare_submission', {'course_id': 'c', 'content_id': 'x'}, False)
        self.assertEqual(result['result']['state'], 'ready')

    async def test_response_body_not_persisted_context_encrypted(self):
        result = await self.dispatch('school_bb_list_courses', {}, False)
        self.assertIn('synthetic private query response', json.dumps(result))
        stored = self.task_data()
        self.assertNotIn('synthetic private query response', json.dumps(stored))
        raw = (Path(self.tmp.name) / 'agent-tasks.sqlite3').read_bytes()
        self.assertNotIn(b'synthetic private task goal', raw)
        self.assertNotIn(b'school_bb_list_courses', raw)
        self.assertNotIn('calls', engine.view(stored))
        self.assertIn('calls', engine.view(stored, True))

    async def test_error_keeps_reference_and_blocks_changed_write(self):
        async def fail(*args, **kwargs):
            raise RuntimeError('synthetic exception must not leak')
        with patch.object(self.server, 'call_tool', side_effect=fail):
            result = await self.dispatch()
        self.assertEqual(result['state'], 'needs_verification')
        self.assertNotIn('synthetic exception', json.dumps(result))
        changed = await self.dispatch(args=self.arguments | {'preparation_id': 'c'*32})
        self.assertFalse(changed['dispatched'])
        read = await self.dispatch('school_bb_submission_status', {'preparation_id': 'a'*32, 'verify': True}, False)
        self.assertEqual(read['result']['state'], 'verified')
        self.assertFalse(engine.view(self.task_data())['write_blocked'])
        self.assertEqual((await self.dispatch())['state'], 'already_attempted')
        self.assertTrue((await self.dispatch(args=self.arguments | {'preparation_id': 'c'*32}))['dispatched'])

    async def test_readback_of_another_plan_never_unblocks_unknown_write(self):
        with patch.object(self.server, 'call_tool', side_effect=RuntimeError('unknown')):
            await self.dispatch()
        await self.dispatch('school_bb_submission_status', {'preparation_id': 'c'*32, 'verify': True}, False)
        self.assertTrue(engine.view(self.task_data())['write_blocked'])
        self.assertFalse((await self.dispatch(args=self.arguments | {'preparation_id': 'c'*32}))['dispatched'])

    def test_new_plan_services_have_exact_status_links(self):
        for service in ('spaces', 'drive', 'vlab'):
            self.assertEqual(engine.links(service, {'plan_id': 'a'*32}),
                [{'tool': f'school_{service}_status', 'arguments': {'plan_id': 'a'*32}}])

    async def test_campus_preparation_auth_failure_allows_resume_without_unlocking_execution(self):
        from school_mcp.drive import operations
        from school_mcp.service_errors import ServiceError
        task=Store().create('drive','synthetic upload',None)['task_id']
        with patch.object(engine,'identity',return_value=None),patch.object(operations,'prepare',side_effect=ServiceError('synthetic',code='authentication_required')):
            result=await engine.call(task,'school_drive_prepare',{'operation':'mkdir','repo_id':'a'*36,'name':'synthetic'})
        self.assertEqual(result['state'],'authentication_required')
        self.assertFalse(engine.view(Store().read(task))['write_blocked'])

    async def test_vlab_pending_receipt_stays_waiting_and_matched_status_unblocks(self):
        from school_mcp.vlab import jobs
        task=Store().create('vlab','synthetic VM task',None)['task_id']
        args={'plan_id':'a'*32,'content_sha256':'b'*64,'authorized':True}
        with patch.object(engine,'identity',return_value=None),patch.object(jobs,'execute',return_value={'state':'executing','plan_id':'a'*32,'result':{'remote_directory':'/synthetic/job'}}):
            result=await engine.call(task,'school_vlab_execute',args,True)
        self.assertEqual(result['state'],'waiting')
        self.assertTrue(engine.view(Store().read(task))['write_blocked'])
        with patch.object(engine,'identity',return_value=None),patch.object(jobs,'status',return_value={'state':'verified','plan_id':'a'*32,'result':{'succeeded':False}}):
            result=await engine.call(task,'school_vlab_status',{'plan_id':'a'*32})
        self.assertFalse(engine.view(Store().read(task))['write_blocked'])
        self.assertFalse(result['result']['result']['succeeded'])

    async def test_authorization_is_forwarded_even_when_model_omits_duplicate_flag(self):
        from school_mcp.vlab import jobs
        task=Store().create('vlab','synthetic job',None)['task_id']
        args={'plan_id':'a'*32,'content_sha256':'b'*64}
        with patch.object(engine,'identity',return_value=None),patch.object(jobs,'execute',return_value={'state':'verified','plan_id':'a'*32}) as execute:
            self.assertFalse((await engine.call(task,'school_vlab_execute',args,False))['dispatched'])
            execute.assert_not_called()
            await engine.call(task,'school_vlab_execute',args,True)
            self.assertTrue(execute.call_args.args[2])

    def test_ready_plan_resolves_failed_call_but_never_a_live_call(self):
        reference={'plan_id':'a'*32}
        for status in ('in_flight','failed_or_unknown'):
            previous={'mode':'execute','status':status,'arguments':reference,'receipt':{}}
            task={'calls':[previous,{}]}
            engine.resolve_verified(task,'vlab','school_vlab_status',reference,{'state':'ready','can_execute':True,**reference},1)
            self.assertEqual('resolved_by' in previous,status=='failed_or_unknown')

    async def test_process_interruption_preserves_claim(self):
        async def interrupt(*args, **kwargs):
            raise asyncio.CancelledError()
        with patch.object(self.server, 'call_tool', side_effect=interrupt):
            with self.assertRaises(asyncio.CancelledError):
                await self.dispatch()
        self.assertTrue(engine.view(self.task_data())['in_flight_or_interrupted'])
        self.assertFalse((await self.dispatch())['dispatched'])
        self.assertEqual(self.count, 0)

    async def test_concurrent_execution_only_dispatches_once(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.server.call_tool
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        with patch.object(self.server, 'call_tool', side_effect=delayed):
            first = asyncio.create_task(self.dispatch())
            await entered.wait()
            second = await self.dispatch()
            self.assertFalse(second['dispatched'])
            release.set()
            await first
        self.assertEqual(self.count, 1)

    async def test_identity_change_blocks_private_task_and_dispatch(self):
        with patch.object(engine, 'adapter', return_value=self.server), patch.object(engine, 'identity', return_value='synthetic-account-a'):
            await engine.call(self.task, 'school_bb_list_courses', {})
        with patch.object(engine, 'identity', return_value='synthetic-account-b'):
            with self.assertRaises(ValueError):
                school_agent_task(self.task, True)
            self.assertEqual(school_agent_tasks()['tasks'], [])
            with self.assertRaises(ValueError):
                await engine.call(self.task, 'school_bb_submit_assignment', self.arguments, True)

    async def test_actual_finance_preflight_routes_and_preserves_missing_guidance(self):
        task = Store().create('finance', 'synthetic travel reimbursement', None)
        with patch.object(engine, 'identity', return_value=None):
            result = await engine.call(task['task_id'], 'school_finance_preflight', {'request': {'business_type': 'travel'}})
        self.assertTrue(result['dispatched'])
        self.assertFalse(result['result']['safe_to_submit'])
        self.assertTrue(result['result']['input_guidance']['fields'])
        self.assertEqual(result['state'], 'needs_input')

    async def test_current_registry_tools_and_real_parameter_schemas(self):
        for key in WORKFLOWS:
            result = await describe(key)
            self.assertEqual(len(result['tools']), len(WORKFLOWS[key]['tools']))
            entry = entry_schema(key)
            actual = await describe(key, entry['name'])
            self.assertEqual(entry['input_schema'], actual['tools'][0]['input_schema'])
        result = await describe('bb', 'school_bb_submit_assignment')
        self.assertEqual(set(result['tools'][0]['input_schema']['required']), {'preparation_id', 'expected_sha256'})
        with self.assertRaises(ValueError):
            await describe('finance', 'school_bb_submit_assignment')

    def test_route_is_advisory_multi_intent_and_agent_first_use_is_offline(self):
        result = route('查成绩并准备报销')
        self.assertEqual({c['workflow'] for c in result['candidates']}, {'academic', 'finance'})
        self.assertFalse(result['automatic_execution'])
        report = first_use(['agent'], check_connections=True)
        self.assertFalse(report['network_checked'])
        self.assertEqual(report['checks'][0]['state'], 'coordinator_ready')


if __name__ == '__main__':
    unittest.main()
