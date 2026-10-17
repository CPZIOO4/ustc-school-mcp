"""Real private-pipe compatibility; no school network or personal directory."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from school_mcp.desktop import native_core
from school_mcp.mail import browser_setup
from school_mcp.bb import email_verification


@unittest.skipUnless(os.name == 'nt' and os.environ.get('SCHOOL_TEST_CORE'), 'built Windows core required')
class NativeMailContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': os.environ['SCHOOL_TEST_CORE'],
                              'SCHOOL_MCP_LOCAL_DIR': self.tmp.name, 'SCHOOL_MAIL_PASSWORD': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(native_core.shutdown)

    def completed_job(self, **params):
        started = native_core.call('mail_bind_begin', {'private_dir': self.tmp.name, **params})
        for _ in range(100):
            result = native_core.call('mail_bind_status', {'job_id': started['job_id']})
            if result['state'] != 'running': return result
            time.sleep(.01)
        self.fail('native mailbox job did not complete')

    def test_python_journal_blocks_rust_creation_and_hides_pending_secret(self):
        for state in ('creation_attempted', 'captured'):
            browser_setup._save({'state': state, 'address': 'synthetic@mail.ustc.edu.cn', 'password': 'fixture-private'})
            result = self.completed_job(address='synthetic@mail.ustc.edu.cn')
            self.assertEqual(result['state'], 'succeeded')
            self.assertEqual(result['result']['state'], state)
            self.assertNotIn('fixture-private', repr(result))
            self.assertEqual(browser_setup._load()['state'], state)

    def test_python_lock_blocks_native_before_any_browser_or_write(self):
        with browser_setup.bind_lock():
            result = self.completed_job(headed=True, create_client_password=True)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['error']['code'], 'mail_bind_busy')
        self.assertFalse((Path(self.tmp.name) / browser_setup.JOURNAL).exists())

    def test_native_message_policy_runs_through_existing_mail_verifier(self):
        message = {'from': 'USTC <id@ustc.edu.cn>', 'to': 'synthetic@mail.ustc.edu.cn',
                   'subject': '统一身份认证验证码', 'body': '验证码：123456'}
        self.assertTrue(email_verification.matches_address('synthetic@mail.ustc.edu.cn', 'sy**@mail.ustc.edu.cn'))
        self.assertEqual(email_verification.extract_code(message, 'synthetic@mail.ustc.edu.cn'), '123456')
        for override in ({'body_truncated': True}, {'body': '验证码：123456；验证码：654321'},
                         {'from': 'id@ustc.edu.cn.evil.example'}, {'body': '重置密码验证码：123456'}):
            self.assertIsNone(email_verification.extract_code({**message, **override}, 'synthetic@mail.ustc.edu.cn'))


if __name__ == '__main__':
    unittest.main()
