"""Native mailbox bridge contracts; all accounts/messages are synthetic."""
from contextlib import nullcontext
import os
import threading
import unittest
from unittest.mock import patch, Mock

from school_mcp.mail import browser_setup, native
from school_mcp.mail.config import MailError
from school_mcp.bb import email_verification


class NativeMailBridgeTests(unittest.TestCase):
    def run_bind(self, call, **options):
        with patch.object(native.native_core, 'call', side_effect=call):
            return native.bind(headed=True, create_client_password=True, resume=False,
                               address='synthetic@mail.ustc.edu.cn', replace_existing=False,
                               timeout=30, notify=Mock(), **options)

    def test_actual_binding_entry_selects_rust_without_python_browser(self):
        with patch.dict(os.environ, {'SCHOOL_MAIL_PASSWORD': ''}), \
             patch.object(native.native_core, 'enabled', return_value=True), \
             patch.object(native, 'bind', return_value={'state': 'captured'}) as bind, \
             patch('school_mcp.browser.chrome_browser') as browser:
            self.assertEqual(browser_setup.run(headed=True, create_client_password=True)['state'], 'captured')
        bind.assert_called_once()
        browser.assert_not_called()

    def test_unknown_generation_result_does_not_retry_or_verify_imap(self):
        calls = []
        def call(op, data):
            calls.append(op)
            return {'job_id': 'test'} if op == 'mail_bind_begin' else {
                'state': 'succeeded', 'result': {'state': 'creation_attempted'}}
        with patch.object(browser_setup, 'finish_pending') as finish:
            result = self.run_bind(call)
        self.assertEqual(result['state'], 'creation_attempted')
        self.assertEqual(calls, ['mail_bind_begin', 'mail_bind_status'])
        finish.assert_not_called()

    def test_captured_secret_is_read_only_from_encrypted_journal(self):
        pending = {'state': 'captured', 'address': 'synthetic@mail.ustc.edu.cn', 'password': 'synthetic-private'}
        def call(op, data):
            return {'job_id': 'test'} if op == 'mail_bind_begin' else {
                'state': 'succeeded', 'result': {'state': 'captured'}}
        with patch.object(browser_setup, 'bind_lock', return_value=nullcontext()), \
             patch.object(browser_setup, '_load', return_value=pending), \
             patch.object(browser_setup, 'finish_pending', return_value={'state': 'connected'}) as finish:
            result = self.run_bind(call)
        finish.assert_called_once_with(pending)
        self.assertNotIn('synthetic-private', repr(result))

    def test_cancel_sent_once_and_no_retry_on_terminal_failure(self):
        stop = threading.Event(); stop.set()
        calls = []
        def call(op, data):
            calls.append(op)
            if op == 'mail_bind_begin': return {'job_id': 'test'}
            if op == 'mail_bind_cancel': return {'stop_requested': True}
            return {'state': 'cancelled', 'error': {'message': 'stopped'}}
        with self.assertRaises(MailError): self.run_bind(call, stopped=stop)
        self.assertEqual(calls, ['mail_bind_begin', 'mail_bind_cancel', 'mail_bind_status'])

    def test_parser_branches_use_private_native_policy(self):
        message = {'from': 'Identity <id@ustc.edu.cn>', 'to': 'synthetic@mail.ustc.edu.cn',
                   'subject': '统一认证验证码', 'body': '验证码：123456'}
        with patch.object(native.native_core, 'enabled', return_value=True), \
             patch.object(native, 'parse', side_effect=[{'matches': True}, {'eligible': True}, {'code': '123456'}]) as parse:
            self.assertTrue(email_verification.matches_address('synthetic@mail.ustc.edu.cn', 'sy**@mail.ustc.edu.cn'))
            self.assertTrue(email_verification.eligible_header(message, 'synthetic@mail.ustc.edu.cn'))
            self.assertEqual(email_verification.extract_code(message, 'synthetic@mail.ustc.edu.cn'), '123456')
        self.assertEqual(parse.call_args.kwargs['senders'], ['id@ustc.edu.cn'])


if __name__ == '__main__':
    unittest.main()
