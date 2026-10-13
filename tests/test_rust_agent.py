"""Native coordinator acceptance using synthetic tasks and adapters only."""
import concurrent.futures
import json
import os
from pathlib import Path
import random
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from school_mcp.agent import engine
from school_mcp.agent.store import Store
from school_mcp.desktop import native_core
from school_mcp.workflow_store import digest
import test_school_agent as legacy

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get('SCHOOL_TEST_CORE', ROOT / 'native/core/target/debug/school-core.exe'))
NATIVE_AVAILABLE = os.name == 'nt' and CORE.is_file()


@unittest.skipUnless(NATIVE_AVAILABLE, 'build Windows native/core first')
class NativeAgentTests(legacy.AgentTests):
    """Run the existing complete behavior suite through the Rust pipe."""
    def setUp(self):
        native_core.shutdown()
        env = patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': str(CORE)})
        env.start()
        self.addCleanup(env.stop)
        super().setUp()
        self.addCleanup(native_core.shutdown)

    def task_data(self):
        return Store().read(self.task)

    async def test_native_path_never_uses_python_sqlite_or_state_helpers(self):
        with patch.object(Store, 'db', side_effect=AssertionError('Python transaction must not run')), \
             patch.object(engine, 'receipt', side_effect=AssertionError('Python receipt must not run')), \
             patch.object(engine, 'resolve_verified', side_effect=AssertionError('Python state must not run')):
            result = await self.dispatch()
            self.assertTrue(result['dispatched'])
            self.assertEqual((await self.dispatch())['state'], 'already_attempted')
            self.assertEqual(Store().read(self.task)['state'], 'verified')
            self.assertEqual(Store().list(0, 10)[0][0]['task_id'], self.task)
            self.assertEqual(Store().create('bb', 'another synthetic task', None)['calls'], [])


@unittest.skipUnless(NATIVE_AVAILABLE, 'build Windows native/core first')
class NativeAgentProtocolTests(unittest.TestCase):
    def setUp(self):
        native_core.shutdown()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': str(CORE), 'SCHOOL_MCP_LOCAL_DIR': self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(native_core.shutdown)
        self.store = Store()
        self.task = self.store.create('bb', '合成任务：Unicode \"测试\"', 'synthetic-account')
        self.args = {'preparation_id': 'a' * 32, 'expected_sha256': 'b' * 64}

    def claim(self, **updates):
        request = dict(task_id=self.task['task_id'], tool='school_bb_submit_assignment',
                       arguments=self.args, authorized=True, current_account='synthetic-account')
        request.update(updates)
        return self.store.native('claim', **request)

    def finish(self, claim, result):
        return self.store.native('finish', task_id=self.task['task_id'], index=claim['index'], key=claim['key'], result=result)

    def test_claim_checks_identity_authorization_and_fixed_registry_itself(self):
        self.assertFalse(self.claim(authorized=False)['claimed'])
        self.assertFalse(self.claim(authorized=False, mode='read')['claimed'])
        for change in ({'current_account': 'different-account'}, {'current_account': None},
                       {'tool': 'school_nan7_publish_offer'}, {'authorized': 1},
                       {'arguments': {'nested': {'PASSWORD': 'synthetic'}}}):
            with self.assertRaises(ValueError):
                self.claim(**change)
        self.assertEqual(self.store.read(self.task['task_id'])['calls'], [])
        claim = self.claim()
        self.assertEqual(claim['mode'], 'execute')
        self.assertEqual(claim['key'], digest(['school_bb_submit_assignment', self.args]))

    def test_python_canonical_numbers_unicode_and_escapes(self):
        rng = random.Random(37)
        numbers = [0.0, -0.0, 1, 1.0, 1e-7, 1e-6, 1e-5, 1e-4, 1e15, 1e16, 1.2345678901234567,
                   2.2250738585072014e-308, 5e-324, 1.7976931348623157e308]
        numbers += [rng.uniform(-1, 1) * (10 ** rng.randint(-300, 300)) for _ in range(128)]
        arguments = {'中': numbers, 'é': '\u0000\b\f\n\r\t"\\😀', 'z': {'β': 1, 'a': 2}}
        claimed = self.claim(tool='school_bb_list_courses', arguments=arguments, authorized=False)
        self.assertEqual(claimed['key'], digest(['school_bb_list_courses', arguments]))
        self.assertEqual(claimed['arguments'], arguments)

    def test_legacy_encrypted_rows_and_original_keys_survive_readback(self):
        # This legacy path is intentional: simulate an already executed Python task.
        with self.store.db() as db:
            task = self.store.load(db, self.task['task_id'])
            task['calls'] = [dict(tool='school_bb_submit_assignment', arguments=self.args,
                key=digest(['school_bb_submit_assignment', self.args]), mode='execute',
                authorized=True, status='failed_or_unknown', receipt={'state': 'uncertain'})]
            task['write_blocked'] = True
            self.store.save(db, task)
        original_key = task['calls'][0]['key']
        self.assertFalse(self.claim()['claimed'])
        read = self.claim(tool='school_bb_submission_status', arguments={'preparation_id': 'a'*32}, authorized=False)
        recovered = self.finish(read, {'state': 'verified', 'preparation_id': 'a'*32})
        self.assertFalse(recovered['write_blocked'])
        self.assertEqual(recovered['calls'][0]['key'], original_key)
        self.assertEqual(self.claim()['result']['state'], 'already_attempted')
        self.assertTrue(self.claim(arguments=self.args | {'preparation_id': 'c'*32})['claimed'])

    def test_legacy_structural_duplicate_blocks_even_if_old_hash_format_differs(self):
        with self.store.db() as db:
            task = self.store.load(db, self.task['task_id'])
            task['calls'] = [dict(tool='school_bb_submit_assignment', arguments=self.args,
                key='legacy-key-retained', mode='execute', authorized=True, status='returned', receipt={'state': 'verified'})]
            self.store.save(db, task)
        self.assertEqual(self.claim()['result']['state'], 'already_attempted')
        self.assertEqual(self.store.read(self.task['task_id'])['calls'][0]['key'], 'legacy-key-retained')

    def test_unknown_receipt_cannot_replay_and_read_bodies_are_not_saved(self):
        claim = self.claim()
        task = self.finish(claim, {'state': 'uncertain', 'preparation_id': 'a'*32, 'body': 'SYNTHETIC-BODY-OMITTED'})
        self.assertTrue(task['write_blocked'])
        self.assertNotIn('SYNTHETIC-BODY-OMITTED', json.dumps(task))
        self.assertFalse(self.claim(arguments=self.args | {'preparation_id': 'c'*32})['claimed'])
        for operation, values in [('finish', {'result': {'state': 'verified'}}), ('failure', {'problem': {'state': 'unavailable'}})]:
            with self.assertRaises(ValueError):
                self.store.native(operation, task_id=self.task['task_id'], index=claim['index'], key=claim['key'], **values)
        ciphertext = self.store.path.read_bytes()
        self.assertNotIn(b'school_bb_submit_assignment', ciphertext)
        self.assertNotIn('合成任务'.encode(), ciphertext)

    def test_failure_risk_comes_from_stored_mode_and_missing_reply_is_durable(self):
        claim = self.claim()
        with self.assertRaises(ValueError):
            self.store.native('finish', task_id=self.task['task_id'], index=claim['index'], key='wrong', result={'state': 'verified'})
        native_core.shutdown()
        self.assertFalse(self.claim()['claimed'])
        task = self.store.native('failure', task_id=self.task['task_id'], index=claim['index'], key=claim['key'],
            problem={'state': 'unavailable', 'raw_exception': 'SYNTHETIC-SECRET'}, mode='read', may_have_written=False)
        self.assertEqual(task['calls'][0]['status'], 'failed_or_unknown')
        self.assertTrue(task['write_blocked'])
        self.assertNotIn('SYNTHETIC-SECRET', json.dumps(task))

    def test_ready_plan_can_clear_failed_claim_but_never_a_live_claim(self):
        self.task = self.store.create('vlab', 'synthetic VM task', 'synthetic-account')
        self.args = {'plan_id': 'a'*32, 'content_sha256': 'b'*64}
        execute = self.claim(tool='school_vlab_execute')
        read_arguments = {'plan_id': 'a'*32}
        read = self.claim(tool='school_vlab_status', arguments=read_arguments, authorized=False)
        live = self.finish(read, {'state': 'ready', 'can_execute': True, **read_arguments})
        self.assertTrue(live['write_blocked'])
        self.assertNotIn('resolved_by', live['calls'][0])
        self.store.native('failure', task_id=self.task['task_id'], index=execute['index'], key=execute['key'],
                          problem={'state': 'needs_verification'})
        read = self.claim(tool='school_vlab_status', arguments=read_arguments, authorized=False)
        recovered = self.finish(read, {'state': 'ready', 'can_execute': True, **read_arguments})
        self.assertFalse(recovered['write_blocked'])
        self.assertEqual(recovered['calls'][0]['resolution'], 'no_write_claimed')
        self.assertEqual(recovered['calls'][0]['key'], execute['key'])
        self.assertEqual(self.claim(tool='school_vlab_execute')['result']['state'], 'already_attempted')

    def test_step_limit_is_enforced_inside_claim_transaction(self):
        with self.store.db() as db:
            task = self.store.load(db, self.task['task_id'])
            task['calls'] = [dict(tool='school_bb_list_courses', arguments={}, key=f'synthetic-{index}',
                mode='read', authorized=False, status='returned', receipt={}) for index in range(99)]
            self.store.save(db, task)
        last = self.claim(tool='school_bb_list_courses', arguments={}, authorized=False)
        self.assertEqual(last['index'], 99)
        stopped = self.claim(tool='school_bb_list_courses', arguments={}, authorized=False)
        self.assertEqual(stopped['result']['state'], 'step_limit')
        self.assertEqual(len(self.store.read(self.task['task_id'])['calls']), 100)

    def test_cross_process_claim_is_atomic(self):
        payload = json.dumps({'operation': 'agent_claim', 'input': dict(database=str(self.store.path.resolve()),
            task_id=self.task['task_id'], tool='school_bb_submit_assignment', arguments=self.args,
            authorized=True, current_account='synthetic-account')}).encode()
        def independent_core(_):
            process = subprocess.run([str(CORE), '--stdio'], input=struct.pack('>I', len(payload)) + payload,
                capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(process.returncode, 0)
            size = struct.unpack('>I', process.stdout[:4])[0]
            reply = json.loads(process.stdout[4:4+size])
            self.assertTrue(reply['ok'])
            return reply['result']['claimed']
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            outcomes = list(executor.map(independent_core, range(4)))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(len(self.store.read(self.task['task_id'])['calls']), 1)

    def test_malformed_legacy_call_fails_without_overwriting_record(self):
        with self.store.db() as db:
            task = self.store.load(db, self.task['task_id'])
            task['calls'] = [42]
            self.store.save(db, task)
        before = self.store.path.read_bytes()
        with self.assertRaises(ValueError):
            self.claim()
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(native_core.call('status', {})['implementation'], 'rust')


if __name__ == '__main__':
    unittest.main()
