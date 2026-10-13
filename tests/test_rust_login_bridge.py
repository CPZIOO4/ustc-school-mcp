"""The Python compatibility entrypoints do not open a second login browser."""
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from school_mcp.desktop import auth, login_native
from school_mcp.campus import login as campus_login
from school_mcp.service_errors import ServiceError


class NativeLoginBridgeTests(unittest.TestCase):
    def test_native_desktop_and_campus_route_before_playwright(self):
        with patch.object(auth, 'load_credentials', side_effect=AssertionError('Python read identity')), \
             patch('school_mcp.desktop.native_core.enabled', return_value=True), \
             patch.object(login_native, 'run', return_value={'connected': True}) as run:
            auth.manual_identity(lambda *_: None)
            auth.manual_icourse(lambda *_: None)
            auth.manual_service('jw', lambda *_: None)
            campus_login.manual('student', lambda *_: None)
        self.assertEqual([c.args[0] for c in run.call_args_list], ['identity', 'icourse', 'jw', 'student'])

    def test_poll_and_cancel_without_secret_fields(self):
        stop = threading.Event()
        calls = []
        def invoke(operation, value):
            calls.append((operation, value))
            if operation == 'login_begin':
                stop.set()
                return {'job_id': 'synthetic'}
            if operation == 'login_cancel':
                return {'stop_requested': True}
            return {'state': 'cancelled', 'error': {'code': 'cancelled', 'message': '已停止'}}
        with patch.object(login_native.native_core, 'call', side_effect=invoke), \
             patch.object(login_native, 'local_dir', return_value=Path('E:/synthetic-profile')):
            with self.assertRaises(ServiceError) as result:
                login_native.run('jw', lambda *_: None, stop)
        self.assertEqual(result.exception.code, 'cancelled')
        self.assertEqual([c[0] for c in calls], ['login_begin', 'login_cancel', 'login_status'])
        self.assertEqual(set(calls[0][1]), {'private_dir', 'service', 'headed', 'timeout_seconds'})

    def test_progress_and_verified_result(self):
        statuses = iter([
            {'state': 'running', 'stage': 'waiting_for_user', 'detail': '官方窗口'},
            {'state': 'running', 'stage': 'waiting_for_user', 'detail': '官方窗口'},
            {'state': 'succeeded', 'result': {'connected': True, 'password_saved': False}},
        ])
        def invoke(operation, _):
            return {'job_id': 'synthetic'} if operation == 'login_begin' else next(statuses)
        progress = []
        with patch.object(login_native.native_core, 'call', side_effect=invoke), \
             patch.object(login_native, 'local_dir', return_value=Path('E:/synthetic-profile')), \
             patch.object(login_native.time, 'sleep'):
            result = login_native.run('icourse', lambda *v: progress.append(v))
        self.assertTrue(result['connected'])
        self.assertEqual(progress, [('waiting_for_user', '官方窗口')])

    def test_poll_failure_requests_cleanup_without_replay(self):
        calls = []
        def invoke(operation, value):
            calls.append(operation)
            if operation == 'login_begin':
                return {'job_id': 'synthetic'}
            if operation == 'login_status':
                raise OSError('pipe closed')
            return {}
        with patch.object(login_native.native_core, 'call', side_effect=invoke), \
             patch.object(login_native, 'local_dir', return_value=Path('E:/synthetic-profile')):
            with self.assertRaises(OSError):
                login_native.run('identity', lambda *_: None)
        self.assertEqual(calls, ['login_begin', 'login_status', 'login_cancel'])
