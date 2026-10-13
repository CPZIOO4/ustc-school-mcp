"""Native assignment IO integration, legacy state compatibility and no fallback."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from school_mcp.bb import native_io, submissions
from school_mcp.bb.session import BBError
from school_mcp.desktop import native_core
from school_mcp.desktop.model import ModelError
import test_bb_submissions as legacy

CORE = Path(os.environ.get('SCHOOL_TEST_CORE', Path(__file__).resolve().parents[1] / 'native/core/target/debug/school-core.exe'))


class NativeBBIORoutingTests(unittest.TestCase):
    def test_prepare_passes_paths_without_reading_python_bytes_or_opening_browser(self):
        with patch.object(submissions.native, 'enabled', return_value=True), \
             patch.object(submissions, 'identity', return_value='synthetic'), \
             patch.object(submissions, 'session_binding', return_value='binding'), \
             patch.object(submissions, 'locate', return_value=legacy.A), \
             patch.object(submissions, 'materials', side_effect=AssertionError('Python byte IO forbidden')), \
             patch.object(submissions, 'Store', side_effect=AssertionError('Python store forbidden')), \
             patch.object(native_io, 'call', return_value={'state': 'ready'}) as call:
            result = submissions.prepare_submission('_1_1', '_2_1', ['C:/synthetic/original.pdf'])
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(call.call_args.args, ('prepare',))
        self.assertEqual(call.call_args.kwargs['files'], ['C:/synthetic/original.pdf'])
        self.assertNotIn('data', str(call.call_args))

    def test_duplicate_submit_returns_original_state_without_locate_or_browser(self):
        state = {'state': 'uncertain', 'content_sha256': 'hash'}
        with patch.object(submissions.native, 'enabled', return_value=True), \
             patch.object(submissions, 'identity', return_value='synthetic'), \
             patch.object(submissions, 'locate', side_effect=AssertionError('No retry query')), \
             patch.object(native_io, 'call', return_value=state) as call:
            self.assertEqual(submissions.submit_assignment('f' * 32, 'hash'), state)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args, ('status',))

    def test_core_error_does_not_trigger_python_submission_fallback(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {'SCHOOL_MCP_LOCAL_DIR': directory}), \
             patch.object(native_core, 'call', side_effect=ModelError('timeout', '本轮已停止')):
            with self.assertRaises(BBError) as caught:
                native_io.call('status', account='synthetic', identifier='f' * 32, verify=False)
            self.assertEqual(caught.exception.code, 'timeout')

    def test_verified_status_needs_no_live_session_even_when_verify_requested(self):
        state = {'state': 'verified', 'phase': 'submit_requested'}
        with patch.object(submissions.native, 'enabled', return_value=True), \
             patch.object(submissions, 'identity', return_value='synthetic'), \
             patch.object(native_io, 'call', return_value=state) as call:
            self.assertEqual(submissions.submission_status('f' * 32, verify=True), state)
        self.assertEqual(call.call_count, 1)
        self.assertFalse(call.call_args.kwargs['verify'])


@unittest.skipUnless(os.name == 'nt' and CORE.is_file(), 'Windows DPAPI and native/core build required')
class NativeBBLegacyStateTests(unittest.TestCase):
    def test_rust_reads_legacy_encrypted_job_and_prevents_old_worker_replay(self):
        native_core.shutdown()
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {'SCHOOL_MCP_LOCAL_DIR': directory}), \
             patch.object(submissions, 'identity', return_value='synthetic'), \
             patch.object(submissions, 'session_binding', return_value='synthetic-session'), \
             patch.object(submissions, 'locate', return_value=legacy.A):
            legacy.FakeBrowser.current = copy.deepcopy(legacy.CURRENT)
            legacy.FakeBrowser.sends = 0
            legacy.FakeBrowser.downloaded = legacy.DATA
            legacy.FakeBrowser.fail = None
            store = submissions.Store(Path(directory))
            # Explicit fake browser/store keeps the fixture offline and uses the
            # original writer to prove real DPAPI/SQLite compatibility.
            with patch.object(submissions.native, 'enabled', return_value=False):
                prepared = submissions.prepare_submission('_1_1', '_2_1', reuse_previous_files=True,
                    resubmit=True, browser_factory=legacy.FakeBrowser, store=store, allow_late=True)
            identifier = prepared['preparation_id']
            with patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': str(CORE)}):
                try:
                    result = native_io.call('status', account='synthetic', identifier=identifier, verify=False)
                    self.assertEqual(result['content_sha256'], prepared['content_sha256'])
                    self.assertEqual(result['files'], prepared['files'])
                    self.assertNotIn('data', result['files'][0])
                    # Existing code may read migrated records. It cannot claim or
                    # modify them without the native transaction lease.
                    self.assertEqual(store.get(identifier)['state'], 'ready')
                    with self.assertRaises(BBError):
                        store.claim(identifier, prepared['content_sha256'])
                    with self.assertRaises(BBError):
                        native_io.call('status', account='another-account', identifier=identifier, verify=False)
                finally:
                    native_core.shutdown()


if __name__ == '__main__':
    unittest.main()
