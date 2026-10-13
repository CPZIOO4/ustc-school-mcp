"""Recovery contracts through the real native pipe, with synthetic failures only."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
from playwright.sync_api import Error as BrowserError
from pydantic import BaseModel, ValidationError

from school_mcp import recovery
from school_mcp.desktop import native_core
from school_mcp.service_errors import ServiceError
from school_mcp.network import CooldownError, PolicyError


SECRET = 'https://private.invalid/?token=synthetic-secret-canary'
CORE = os.environ.get('SCHOOL_NATIVE_CORE_TEST') or os.environ.get('SCHOOL_NATIVE_CORE')
ID = 'ERR-0123456789ABCDEF'


class RecoveryContractTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, SCHOOL_NATIVE_CORE=''))

    def test_typed_causes_are_bounded_and_do_not_classify_prose(self):
        wrapped = RuntimeError(SECRET)
        wrapped.__cause__ = ServiceError(SECRET, code='authentication_required')
        self.assertEqual(recovery.classify(wrapped), ('authentication_required', None))
        wrapped.__cause__ = wrapped
        self.assertEqual(recovery.classify(wrapped), ('unavailable', None))
        chain = ServiceError(SECRET, code='authentication_required')
        for _ in range(8):
            parent = RuntimeError(SECRET)
            parent.__cause__, chain = chain, parent
        self.assertEqual(recovery.classify(chain), ('unavailable', None))
        self.assertEqual(recovery.classify(RuntimeError('authentication_required ' + SECRET)),
                         ('unavailable', None))
        self.assertEqual(recovery.failure(ServiceError(SECRET, code=[]))['error_code'], 'unavailable')

    def test_retry_edge_cases_remain_safe_and_do_not_replace_original_error(self):
        for seconds in (float('nan'), float('inf'), -float('inf'), 10**400, -1, 0,
                        True, False, '12', [], {}, None):
            with self.subTest(seconds=type(seconds).__name__):
                result = recovery.failure(code='cooldown', retry_after_seconds=seconds)
                self.assertEqual(result['error_code'], 'cooldown')
                self.assertNotIn('retry_after_seconds', result)
                json.dumps(result, allow_nan=False)
        for seconds, expected in ((0.01, 1), (12.01, 13), (13, 13), (2**64 - 1, 2**64 - 1)):
            result = recovery.failure(code='cooldown', retry_after_seconds=seconds)
            self.assertEqual(result['retry_after_seconds'], expected)
        result = recovery.failure(code='cooldown', retry_after_seconds=1e300)
        self.assertEqual(result['retry_after_seconds'], 1e300)
        result = recovery.failure(code='cooldown', retry_after_seconds=2**64 + 1)
        self.assertGreaterEqual(result['retry_after_seconds'], 2**64 + 1)

    def test_malformed_failure_metadata_cannot_enable_retry_or_leak_prose(self):
        original = {'state': 'uncertain', 'error_code': 'timeout', 'operation_failed': True,
                    'next_step': SECRET, 'body': SECRET,
                    'recovery': {'automatic_retry': True, 'retry_write_allowed': True},
                    'error_id': ID, 'diagnostic_saved': False}
        result = recovery.returned_failure(original)
        self.assertEqual(result['state'], 'needs_verification')
        self.assertEqual(result['error_code'], 'timeout')
        self.assertEqual(result['recovery']['action'], 'verify_original_receipt')
        self.assertFalse(result['recovery']['automatic_retry'])
        self.assertFalse(result['recovery']['retry_write_allowed'])
        self.assertFalse(result['recovery']['retry_read_after_check'])
        self.assertEqual(result['error_id'], ID)
        self.assertFalse(result['diagnostic_saved'])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(original['next_step'], SECRET)
        original['diagnostic_note'] = recovery.DIAGNOSTIC_UNAVAILABLE
        self.assertEqual(recovery.returned_failure(original)['diagnostic_note'], recovery.DIAGNOSTIC_UNAVAILABLE)
        original['diagnostic_note'] = SECRET
        self.assertNotIn('diagnostic_note', recovery.returned_failure(original))
        for identifier, saved in ((ID.lower(), True), (ID + '\n', True), (SECRET, True),
                                  (ID, 1), (ID, 'true')):
            result = recovery.returned_failure({'state': 'timeout', 'error_id': identifier,
                                                'diagnostic_saved': saved})
            self.assertNotIn('error_id', result)
            self.assertNotIn('diagnostic_saved', result)

    def test_native_input_excludes_private_fields_and_invalid_json_numbers(self):
        with patch.object(native_core, 'enabled', return_value=True), \
                patch.object(native_core, 'call', return_value={'native': True}) as call:
            result = recovery.failure(ServiceError(SECRET, code='cooldown',
                retry_after_seconds=float('nan')), service=SECRET, write_may_have_run=True)
            self.assertEqual(result, {'native': True})
            operation, payload = call.call_args.args
            self.assertEqual(operation, 'recovery_failure')
            self.assertEqual(payload, {'code': 'cooldown', 'service': None,
                'write_may_have_run': True, 'retry_after_seconds': None})
            json.dumps(payload, allow_nan=False)
            recovery.returned_failure({'state': 'cooldown', 'body': SECRET,
                'retry_after_seconds': float('inf'), 'error_id': SECRET,
                'diagnostic_saved': True, 'recovery': {'action': SECRET}}, service=SECRET)
            operation, payload = call.call_args.args
            self.assertEqual(operation, 'recovery_returned_failure')
            self.assertNotIn(SECRET, json.dumps(payload, allow_nan=False))
            self.assertEqual(payload['value']['retry_after_seconds'], None)


@unittest.skipUnless(CORE and Path(CORE).is_file(), 'Set SCHOOL_NATIVE_CORE_TEST to the built core executable')
class NativePipeParityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, SCHOOL_NATIVE_CORE=str(Path(CORE).resolve())))
        self.addCleanup(native_core.shutdown)

    def compare(self, function, *args, **kwargs):
        with patch.object(native_core, 'enabled', return_value=False):
            expected = function(*args, **kwargs)
        actual = function(*args, **kwargs)
        self.assertEqual(actual, expected)
        self.assertNotIn(SECRET, json.dumps(actual))
        return actual

    def test_every_fixed_reason_and_write_contract_matches_through_pipe(self):
        status = native_core.call('status', {})
        self.assertIn('recovery_failure', status['capabilities'])
        for code in recovery.REASONS:
            for write in (False, True):
                with self.subTest(code=code, write=write):
                    result = self.compare(recovery.failure, code=code, service='bb',
                                          write_may_have_run=write, retry_after_seconds=17.25)
                    self.assertFalse(result['recovery']['automatic_retry'])
                    self.assertFalse(result['recovery']['retry_write_allowed'])
                    self.assertEqual(self.compare(recovery.returned_failure, result), result)

    def test_all_typed_python_adapters_reach_native_fixed_contracts(self):
        class Arguments(BaseModel):
            count: int
        try:
            Arguments(count='bad')
        except ValidationError as error:
            validation = error
        examples = [(TimeoutError(SECRET), 'timeout'), (httpx.ReadTimeout(SECRET), 'timeout'),
            (ConnectionError(SECRET), 'network_unavailable'),
            (httpx.ConnectError(SECRET), 'network_unavailable'),
            (PolicyError(SECRET), 'local_policy_unavailable'),
            (CooldownError(12.5), 'cooldown'), (validation, 'invalid_arguments'),
            (BrowserError(SECRET), 'runtime_failure'), (RuntimeError(SECRET), 'unavailable')]
        for status, code in ((401, 'authentication_required'), (403, 'access_denied'),
                             (408, 'timeout'), (429, 'cooldown'), (503, 'service_unavailable')):
            examples.append((httpx.HTTPStatusError(SECRET, request=httpx.Request('GET', SECRET),
                response=httpx.Response(status)), code))
        for error, code in examples:
            with self.subTest(code=code):
                wrapped = RuntimeError(SECRET)
                wrapped.__cause__ = error
                result = self.compare(recovery.failure, wrapped, service='bb', write_may_have_run=True)
                self.assertEqual(result['error_code'], code)
                self.assertEqual(result['recovery']['action'], 'verify_original_receipt')

    def test_result_states_successes_unknowns_and_safe_diagnostics_match(self):
        states = list(recovery.REASONS) + ['waiting_for_verification', 'authentication_failed',
            'needs_verification', 'uncertain', 'already_attempted', 'connected', SECRET, None, [], {}]
        for state in states:
            for saved in (True, False, 1):
                self.compare(recovery.returned_failure, {'state': state, 'body': SECRET,
                    'retry_after_seconds': 3.2, 'error_id': ID, 'diagnostic_saved': saved}, service='bb')
        for value in (None, [], '', 1, False, {}, {'connected': False}, {'connected': 0},
                      {'connected': True}, {'note': 'authentication_required ' + SECRET}):
            self.compare(recovery.returned_failure, value)
        self.assertIsNone(recovery.returned_failure({'body': 'authentication_required ' + SECRET}))

    def test_retry_values_cannot_break_the_json_pipe(self):
        for seconds in (float('nan'), float('inf'), -float('inf'), 10**400, None,
                        [], {}, True, False, '12', -5, 0, 0.01, 17.2, 2**64 - 1, 2**64 + 1, 1e300):
            self.compare(recovery.failure, code='cooldown', retry_after_seconds=seconds)
            self.compare(recovery.returned_failure, {'state': 'cooldown', 'retry_after_seconds': seconds})

    def test_raw_native_result_revalidates_untrusted_recovery_fields(self):
        original = {'state': 'needs_verification', 'error_code': 'timeout', 'operation_failed': True,
            'next_step': SECRET, 'body': SECRET, 'error_id': ID, 'diagnostic_saved': False,
            'recovery': {'action': 'retry', 'automatic_retry': True, 'retry_write_allowed': True}}
        result = native_core.call('recovery_returned_failure', {'value': original})
        self.assertEqual(result, recovery.returned_failure(original))
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertFalse(result['recovery']['automatic_retry'])
        self.assertFalse(result['recovery']['retry_write_allowed'])
        original['diagnostic_note'] = recovery.DIAGNOSTIC_UNAVAILABLE
        result = self.compare(recovery.returned_failure, original)
        self.assertEqual(result['diagnostic_note'], recovery.DIAGNOSTIC_UNAVAILABLE)


if __name__ == '__main__':
    unittest.main()
