from __future__ import annotations

import base64
import os
import smtplib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import Mock, patch

from school_mcp.mail import outbox
from school_mcp.mail.config import MailConfig, MailError
from school_mcp.mail.client import MailClient
from school_mcp.network import CooldownError, PolicyError
from test_mail import FakeIMAP, sample_mail


class FakeSMTP:
    def __init__(self):
        self.esmtp_features = {'auth': 'LOGIN PLAIN'}
        self.auth_plain = Mock()
        self.auth_login = Mock()
        self.auth = Mock(return_value=(235, b'ok'))
        self.ehlo_or_helo_if_needed = Mock()
        self.mail = Mock(return_value=(250, b'ok'))
        self.rcpt = Mock(return_value=(250, b'ok'))
        self.data = Mock(return_value=(250, b'ok'))
        self.close = Mock()


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = outbox.Outbox(self.root)
        self.config = MailConfig('student@mail.ustc.edu.cn')
        self.smtp = FakeSMTP()
        # Portable behavioral tests; the real Windows DPAPI test below is separate.
        for name, kwargs in [
            ('school_mcp.mail.outbox._dpapi', {'side_effect': lambda data, decrypt=False: base64.b64decode(data) if decrypt else base64.b64encode(data)}),
            ('school_mcp.mail.outbox.load_password', {'return_value': 'synthetic-password'}),
            ('school_mcp.mail.outbox.smtplib.SMTP_SSL', {'return_value': self.smtp}),
            ('school_mcp.network.limiter', {}),
        ]:
            p = patch(name, **kwargs)
            m = p.start()
            self.addCleanup(p.stop)
            if name.endswith('SMTP_SSL'):
                self.connector = m
            if name.endswith('limiter'):
                self.limiter = m

    def prepare(self, **kwargs):
        args = dict(to=['recipient@example.com'], subject='合成测试', body='Synthetic body')
        args.update(kwargs)
        return outbox.prepare(self.config, store=self.store, **args)

    def send(self, draft):
        return outbox.send(self.config, draft['draft_id'], draft['content_sha256'], store=self.store)

    def test_missing_fields_have_deterministic_next_action_and_no_network(self):
        result = self.prepare(to=[], subject='', body='')
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual({x['field'] for x in result['issues']}, {'to', 'subject', 'body'})
        self.assertNotIn('draft_id', result)
        self.connector.assert_not_called()

    def test_header_injection_and_non_bare_address_are_rejected(self):
        for value in ['a@example.com\r\nBcc: b@example.com', 'Name <a@example.com>', 'a..b@example.com', '.a@example.com', 'a@-example.com']:
            self.assertEqual(self.prepare(to=[value])['status'], 'needs_input')
        self.assertEqual(self.prepare(subject='x\r\nBcc: y')['status'], 'needs_input')

    def test_bcc_only_in_envelope_and_duplicate_recipients_removed(self):
        draft = self.prepare(cc=['recipient@example.com'], bcc=['hidden@example.com'])
        self.send(draft)
        raw = self.smtp.data.call_args.args[0]
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertIsNone(msg['Bcc'])
        self.assertNotIn(b'hidden@example.com', raw)
        self.assertEqual(self.smtp.rcpt.call_count, 2)

    def test_attachment_is_frozen_even_if_original_changes_or_is_removed(self):
        attachment = self.root / 'synthetic.txt'
        attachment.write_bytes(b'original')
        draft = self.prepare(attachments=[str(attachment)])
        attachment.write_bytes(b'changed')
        attachment.unlink()
        self.send(draft)
        msg = BytesParser(policy=policy.default).parsebytes(self.smtp.data.call_args.args[0])
        self.assertEqual(next(msg.iter_attachments()).get_payload(decode=True), b'original')
        self.assertNotIn(str(self.root), str(draft['preview']))

    def test_missing_attachment_and_recipient_limit(self):
        self.assertEqual(self.prepare(attachments=['relative.txt'])['status'], 'needs_input')
        self.assertEqual(self.prepare(to=['a@example.com'] * 21)['status'], 'needs_input')

    def test_size_limit_includes_mime_encoding(self):
        with patch.object(outbox, 'MAX_BYTES', 100):
            self.assertEqual(self.prepare()['status'], 'needs_input')

    def test_same_draft_has_only_one_network_attempt(self):
        draft = self.prepare()
        first, second = self.send(draft), self.send(draft)
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'accepted')
        self.assertFalse(first['delivery_confirmed'])
        self.smtp.data.assert_called_once()
        self.connector.assert_called_once()

    def test_cross_process_gate_competes_atomically(self):
        draft = self.prepare()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: outbox.Outbox(self.root).claim(draft['draft_id']), range(12)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.send(draft)['status'], 'sending')
        self.connector.assert_not_called()

    def test_digest_and_account_mismatch_block_send(self):
        draft = self.prepare()
        with self.assertRaises(MailError):
            outbox.send(self.config, draft['draft_id'], 'wrong', store=self.store)
        with self.assertRaises(MailError):
            outbox.send(MailConfig('other@mail.ustc.edu.cn'), draft['draft_id'], draft['content_sha256'], store=self.store)
        self.connector.assert_not_called()

    def test_partial_delivery_never_retries_accepted_recipients(self):
        draft = self.prepare(to=['a@example.com', 'b@example.com'])
        self.smtp.rcpt.side_effect = [(250, b'ok'), (550, b'synthetic refusal')]
        result = self.send(draft)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['accepted_recipients'], ['a@example.com'])
        self.assertEqual(result['refused_recipients'], [{'address': 'b@example.com', 'smtp_code': 550}])
        self.send(draft)
        self.smtp.data.assert_called_once()

    def test_all_refused_does_not_transmit_body(self):
        self.smtp.rcpt.return_value = (550, b'synthetic refusal')
        result = self.send(self.prepare())
        self.assertEqual(result['status'], 'rejected')
        self.smtp.data.assert_not_called()

    def test_sender_rejection_stops_before_recipients(self):
        self.smtp.mail.return_value = (550, b'synthetic refusal')
        self.assertEqual(self.send(self.prepare())['status'], 'rejected')
        self.smtp.rcpt.assert_not_called()

    def test_explicit_data_negative_response_is_rejected(self):
        self.smtp.data.side_effect = smtplib.SMTPDataError(554, b'synthetic sensitive response')
        result = self.send(self.prepare())
        self.assertEqual(result['status'], 'rejected')
        self.assertNotIn('sensitive response', str(result))

    def test_disconnect_during_data_is_unknown_not_retryable(self):
        self.smtp.data.side_effect = smtplib.SMTPServerDisconnected('synthetic sensitive response')
        draft = self.prepare()
        result = self.send(draft)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['next_action'], 'verify_with_recipient_do_not_resend')
        self.send(draft)
        self.smtp.data.assert_called_once()

    def test_connection_failure_is_known_before_data(self):
        self.connector.side_effect = TimeoutError()
        self.assertEqual(self.send(self.prepare())['status'], 'failed_before_data')

    def test_authentication_uses_one_mechanism_then_cooldown(self):
        self.smtp.auth.side_effect = smtplib.SMTPAuthenticationError(535, b'synthetic refusal')
        result = self.send(self.prepare())
        self.assertEqual(result['error_code'], 'authentication_failed')
        self.smtp.auth.assert_called_once()
        self.smtp.mail.assert_not_called()
        self.limiter.failure.assert_called_once_with('smtp', authentication=True)

    def test_close_failure_cannot_undo_data_acceptance(self):
        self.smtp.close.side_effect = OSError()
        self.assertEqual(self.send(self.prepare())['status'], 'accepted')

    def test_receipt_write_failure_does_not_allow_resend(self):
        draft = self.prepare()
        with patch.object(self.store, 'finish', side_effect=MailError('synthetic failure')):
            self.assertEqual(self.send(draft)['status'], 'unknown')
        self.assertEqual(self.send(draft)['status'], 'sending')
        self.smtp.data.assert_called_once()

    def test_claim_failure_prevents_network(self):
        draft = self.prepare()
        with patch.object(self.store, 'claim', side_effect=MailError('synthetic failure')):
            with self.assertRaises(MailError):
                self.send(draft)
        self.connector.assert_not_called()

    def test_cooldown_keeps_same_draft_ready(self):
        self.limiter.acquire.side_effect = CooldownError(30)
        draft = self.prepare()
        result = self.send(draft)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['retry_after_seconds'], 30)
        self.connector.assert_not_called()

    def test_pacing_failure_after_acceptance_keeps_send_result(self):
        self.limiter.success.side_effect = PolicyError('synthetic failure')
        self.assertEqual(self.send(self.prepare())['status'], 'accepted')

    def test_status_defaults_to_no_content(self):
        draft = self.prepare()
        result = outbox.status(draft['draft_id'], store=self.store)
        self.assertNotIn('preview', result)
        self.assertNotIn('Synthetic body', str(result))
        self.connector.assert_not_called()

    def test_reply_uses_reply_to_and_exact_parent_not_all_recipients(self):
        client = Mock(config=self.config)
        client.read.return_value = {'reply-to': 'Office <reply@example.com>', 'from': 'original@example.com',
                                  'to': 'many@example.com', 'cc': 'other@example.com', 'subject': 'Topic',
                                  'message-id': '<synthetic-parent@example.com>', 'body': 'untrusted'}
        result = outbox.prepare_reply(client, uid=42, uid_validity=123, body='Reply text', store=self.store)
        self.assertEqual(result['preview']['to'], ['reply@example.com'])
        self.assertEqual(result['preview']['cc'], [])
        self.assertEqual(result['preview']['body'], 'Reply text')
        self.send(result)
        msg = BytesParser(policy=policy.default).parsebytes(self.smtp.data.call_args.args[0])
        self.assertEqual(msg['In-Reply-To'], '<synthetic-parent@example.com>')
        client.read.assert_called_once_with(42, 123, mailbox='INBOX', max_chars=1, include_headers=True)

    def test_reply_ambiguous_target_stops(self):
        client = Mock(config=self.config)
        client.read.return_value = {'reply-to': 'a@example.com, b@example.com'}
        self.assertEqual(outbox.prepare_reply(client, uid=42, uid_validity=123, body='Reply', store=self.store)['status'], 'needs_input')

    def test_find_replies_exact_thread_match_preserves_unread(self):
        fake = FakeIMAP()
        fake.raw = b'In-Reply-To: <synthetic-parent@example.com>\r\nSubject: Re: Test\r\n\r\n'
        client = MailClient(self.config, 'synthetic-password')
        with patch('school_mcp.mail.client.imaplib.IMAP4_SSL', return_value=fake):
            result = client.find_replies('<synthetic-parent@example.com>')
            self.assertEqual(result['status'], 'replies_found')
            self.assertEqual(result['messages'][0]['uid_validity'], 123)
            fake.raw = b'In-Reply-To: <synthetic-parent@example.com.extra>\r\n\r\n'
            self.assertEqual(client.find_replies('<synthetic-parent@example.com>')['status'], 'no_reply_found')
        self.assertTrue(all(call[2] is True for call in fake.calls if call[0] == 'SELECT'))
        self.assertTrue(all('BODY.PEEK' in call[-1] for call in fake.calls if call[0] == 'FETCH'))

    def test_find_replies_handles_reference_chain_and_truncation(self):
        fake = FakeIMAP()
        fake.raw = b'References: <earlier@example.com> <synthetic-parent@example.com>\r\n\r\n'
        with patch('school_mcp.mail.client.imaplib.IMAP4_SSL', return_value=fake):
            result = MailClient(self.config, 'synthetic-password').find_replies('<synthetic-parent@example.com>', limit=1)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['messages']), 1)

    def test_reply_stops_when_server_returns_a_different_uid(self):
        fake = FakeIMAP()
        original_uid = fake.uid
        def wrong_uid(command, *args):
            code, data = original_uid(command, *args)
            if command == 'FETCH' and 'BODY.PEEK' in args[-1]:
                data[0] = (data[0][0].replace(b'UID 42', b'UID 99'), data[0][1])
            return code, data
        fake.uid = wrong_uid
        with patch('school_mcp.mail.client.imaplib.IMAP4_SSL', return_value=fake):
            with self.assertRaises(MailError):
                outbox.prepare_reply(MailClient(self.config, 'synthetic-password'), uid=42, uid_validity=123,
                                     body='Reply', store=self.store)
        self.connector.assert_not_called()


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI integration')
class DPAPIOutboxTests(unittest.TestCase):
    def test_real_encrypted_storage_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            store = outbox.Outbox(Path(directory))
            result = outbox.prepare(MailConfig('student@mail.ustc.edu.cn'), to=['recipient@example.com'],
                                    subject='synthetic encrypted subject', body='synthetic private body', store=store)
            raw = store.path.read_bytes()
            for secret in (b'recipient@example.com', b'synthetic private body', b'student@mail.ustc.edu.cn'):
                self.assertNotIn(secret, raw)
            restored = outbox.status(result['draft_id'], include_preview=True, store=outbox.Outbox(Path(directory)))
            self.assertEqual(restored['preview']['body'], 'synthetic private body')


if __name__ == '__main__':
    unittest.main()
