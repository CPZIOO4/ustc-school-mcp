"""Private adapter framing and failure safety; no school or provider requests."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from school_mcp.desktop.legacy_worker import Stop, dispatch, business_lock


class LegacyWorkerTests(unittest.TestCase):
    def test_fixed_dispatch_never_interprets_url_or_module(self):
        for action in ('https://example.test/', 'os.system', 'school_mcp.desktop.server'):
            with self.assertRaises(ValueError):
                dispatch(None, action, {}, Stop(Path('uncreated-marker')), lambda *_: None)

    def test_login_restoration_is_not_delegated_to_python(self):
        with patch('school_mcp.desktop.connections.connect', return_value={'connected': False}) as connect:
            result = dispatch(None, 'service_check', {'service': 'bb', 'force': True, 'restore': True}, None, None)
        self.assertFalse(result['connected'])
        self.assertEqual(connect.call_args.kwargs, {'force': False, 'restore': False})

    def test_mail_binding_progress_contract(self):
        events = []
        def run(**kwargs):
            kwargs['notify']({'state': 'waiting', 'next_step': 'synthetic'})
            return {'state': 'waiting'}
        with patch('school_mcp.mail.browser_setup.run', side_effect=run):
            dispatch(None, 'mail_bind', {'resume': True}, None, lambda *v: events.append(v))
        self.assertEqual(events, [('waiting', 'synthetic')])

    def test_private_worker_reuses_process_and_rejects_root_switch_and_unknown_action(self):
        with tempfile.TemporaryDirectory(prefix='school-adapter-test-') as tmp:
            env = dict(os.environ)
            env.pop('SCHOOL_NATIVE_CORE', None)
            env['SCHOOL_MCP_LOCAL_DIR'] = str(Path(tmp) / 'profile')
            process = subprocess.Popen([sys.executable, '-X', 'utf8', '-m', 'school_mcp.desktop.legacy_worker'],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            def call(action, root=tmp):
                value = {'root': root, 'stop_name': '.adapter-stop-' + 'a' * 32, 'action': action, 'input': {}}
                raw = json.dumps(value).encode()
                process.stdin.write(struct.pack('>I', len(raw)) + raw)
                process.stdin.flush()
                size = struct.unpack('>I', process.stdout.read(4))[0]
                self.assertLessEqual(size, 16 * 1024 * 1024)
                return json.loads(process.stdout.read(size))
            try:
                self.assertEqual(len(call('campus_sites')['result']['sites']), 17)
                self.assertIs(call('metadata')['result']['mail']['configured'], False)
                with business_lock(tmp) as held:
                    self.assertTrue(held)
                    self.assertTrue(call('background_status')['result']['adapter_running'])
                self.assertFalse(call('background_status')['result']['adapter_running'])
                self.assertIsNone(process.poll())
                failure = call('campus_sites', root=str(Path(tmp) / 'another-scope'))
                self.assertFalse(failure['ok'])
                self.assertNotIn('another-scope', str(failure))
                self.assertFalse(call('arbitrary-command')['ok'])
                self.assertEqual(len(call('campus_sites')['result']['sites']), 17)
            finally:
                process.stdin.close()
                process.wait(15)
                errors = process.stderr.read()
                process.stdout.close()
                process.stderr.close()
            self.assertEqual(process.returncode, 0, errors.decode(errors='replace'))


if __name__ == '__main__':
    unittest.main()
