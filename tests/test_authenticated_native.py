"""Bridge acceptance: no Python network and no replay after a request was sent."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from school_mcp import authenticated_native as bridge
from school_mcp.desktop import native_core
from school_mcp.desktop.model import ModelError
from school_mcp.jw.session import JWError

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get('SCHOOL_TEST_CORE', ROOT / 'native/core/target/debug/school-core.exe'))


class BridgeTests(unittest.TestCase):
    def test_decoded_response_is_only_a_memory_container(self):
        result = {'state': 'response', 'status': 200, 'url': 'https://jw.ustc.edu.cn/home',
                  'headers': {'content-type': 'text/html; charset=utf-8'}, 'text': '合成教务页面'}
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, 'local_dir', return_value=Path(directory)), \
             patch.object(native_core, 'call', return_value=result) as call, \
             patch.object(bridge.httpx, 'Client', side_effect=AssertionError('Python network used')):
            response = bridge.request('jw', '/home', [], 'synthetic-reader', JWError)
        self.assertEqual(response.text, '合成教务页面')
        self.assertEqual(call.call_args.args[0], 'authenticated_read')
        self.assertEqual(call.call_count, 1)

    def test_only_unsent_reservation_wait_can_repeat(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, 'local_dir', return_value=Path(directory)), \
             patch.object(native_core, 'call', side_effect=[{'state': 'wait', 'retry_after_seconds': 0},
                 {'state': 'cooldown', 'retry_after_seconds': 20}]) as call:
            with self.assertRaises(JWError) as caught:
                bridge.request('jw', '/home', [], 'synthetic-reader', JWError)
        self.assertEqual(caught.exception.code, 'cooldown')
        self.assertEqual(call.call_count, 2)

    def test_native_error_never_falls_back_or_repeats(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, 'local_dir', return_value=Path(directory)), \
             patch.object(native_core, 'call', side_effect=ModelError('runtime_failure', '本轮已停止。')) as call:
            with self.assertRaises(JWError) as caught:
                bridge.request('jw', '/home', [], 'synthetic-reader', JWError)
        self.assertEqual(caught.exception.code, 'runtime_failure')
        self.assertEqual(call.call_count, 1)

    def test_private_directory_failure_does_not_expose_path(self):
        with patch.object(bridge, 'local_dir', side_effect=OSError('synthetic-private-path')), patch.object(native_core, 'call') as call:
            with self.assertRaises(JWError) as caught:
                bridge.request('jw', '/home', [], 'synthetic-reader', JWError)
        self.assertEqual(caught.exception.code, 'local_policy_unavailable')
        self.assertNotIn('synthetic-private-path', str(caught.exception))
        call.assert_not_called()

    @unittest.skipUnless(CORE.is_file(), 'build native/core first')
    def test_real_core_refuses_write_paths_before_any_network(self):
        native_core.shutdown()
        self.addCleanup(native_core.shutdown)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': str(CORE)}):
            for service, path, method in [('jw', '/for-std/course-select/drop', 'POST'),
                                          ('bb', '/webapps/assignment/uploadAssignment', 'POST')]:
                with self.assertRaises(ModelError) as caught:
                    native_core.call('authenticated_read', dict(service=service, path=path, method=method,
                        cookies=[], params={}, user_agent='synthetic-reader', database=str(Path(directory)/'policy.db')))
                self.assertEqual(caught.exception.code, 'invalid_arguments')
            self.assertFalse((Path(directory)/'policy.db').exists())


if __name__ == '__main__':
    unittest.main()
