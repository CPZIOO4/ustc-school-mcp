from __future__ import annotations

import base64
import imaplib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from school_mcp.mail import outbox, sent
from school_mcp.mail.client import MailClient
from school_mcp.mail.config import MailConfig, MailError
from school_mcp.mail.parsing import parse_email
from test_mail import FakeIMAP


class SentIMAP(FakeIMAP):
    def __init__(self):
        super().__init__()
        self.listing = [b'(\\Sent) "/" "Sent Items"', b'() "/" "INBOX"']
        self.messages = {}
        self.append_calls = []
        self.append_error = None
        self.append_code = 'OK'
        self.search_count = 0
        self.fail_after_append = False
        self.hide_after_append = False

    def list(self):
        return 'OK', self.listing

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == 'SEARCH':
            self.search_count += 1
            if self.append_calls and self.fail_after_append:
                raise imaplib.IMAP4.error('synthetic failure')
            if self.append_calls and self.hide_after_append:
                return 'OK', [b'']
            return 'OK', [' '.join(str(x) for x in self.messages).encode()]
        uid = int(args[0])
        return 'OK', [(f'1 (UID {uid})'.encode(), self.messages[uid])]

    def append(self, mailbox, flags, date, raw):
        self.append_calls.append((mailbox, flags, date, raw))
        if self.append_error:
            raise self.append_error
        if self.append_code == 'OK':
            self.messages[100] = raw
        return self.append_code, [b'synthetic response']


class SentCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = outbox.Outbox(Path(self.temp.name))
        self.config = MailConfig('student@mail.ustc.edu.cn')
        self.client = MailClient(self.config, 'synthetic-password')
        self.imap = SentIMAP()
        for name, kwargs in [
            ('school_mcp.mail.outbox._dpapi', {'side_effect': lambda data, decrypt=False: base64.b64decode(data) if decrypt else base64.b64encode(data)}),
            ('school_mcp.mail.client.imaplib.IMAP4_SSL', {'return_value': self.imap}),
            ('school_mcp.network.limiter', {}),
        ]:
            p = patch(name, **kwargs)
            m = p.start()
            self.addCleanup(p.stop)
            if name.endswith('limiter'):
                self.limiter = m
        draft = outbox.prepare(self.config, to=['recipient@example.com'], subject='synthetic sent test', body='synthetic body', store=self.store)
        self.identifier = draft['draft_id']
        _, self.payload = self.store.get(self.identifier)
        self.store.finish(self.identifier, 'accepted', self.payload)

    def save(self, mailbox=''):
        return sent.save(self.client, self.identifier, mailbox, store=self.store)

    def check(self, mailbox=''):
        return sent.check(self.client, self.identifier, mailbox, store=self.store)

    def test_check_missing_is_read_only(self):
        self.assertEqual(self.check()['status'], 'missing')
        self.assertEqual(self.imap.append_calls, [])
        self.assertIsNone(self.store.copy_attempt(self.identifier))

    def test_present_copy_prevents_append(self):
        self.imap.messages[12] = base64.b64decode(self.payload['raw'])
        result = self.save()
        self.assertEqual(result['status'], 'present')
        self.assertEqual(result['copies'][0]['uid'], 12)
        self.assertEqual(result['copies'][0]['uid_validity'], 123)
        self.assertEqual(self.imap.append_calls, [])

    def test_missing_copy_appends_once_then_checks_with_peek(self):
        self.assertEqual(self.save()['status'], 'saved')
        self.assertEqual(self.save()['status'], 'present')
        self.assertEqual(len(self.imap.append_calls), 1)
        self.assertEqual(self.store.copy_attempt(self.identifier)['state'], 'saved')
        self.assertEqual(parse_email(self.imap.append_calls[0][3])['message-id'], self.payload['message_id'])
        self.assertEqual(self.imap.append_calls[0][:2], ('"Sent Items"', '(\\Seen)'))
        self.assertTrue(all(c[2] for c in self.imap.calls if c[0] == 'SELECT'))
        self.assertTrue(all('BODY.PEEK' in c[-1] for c in self.imap.calls if c[0] == 'FETCH'))
        self.limiter.acquire.assert_any_call('mail')

    def test_partial_send_can_be_archived_but_not_inferred_delivered(self):
        self.store.finish(self.identifier, 'partial', self.payload)
        self.assertEqual(self.save()['status'], 'saved')
        self.assertFalse(self.check()['delivery_confirmed'])

    def test_unknown_or_unsent_never_archived(self):
        for state in ('ready', 'sending', 'unknown', 'failed_before_data', 'rejected'):
            self.store.finish(self.identifier, state, self.payload)
            self.assertEqual(self.save()['status'], 'not_eligible')
        self.assertEqual(self.imap.append_calls, [])

    def test_existing_duplicates_reported_not_deleted(self):
        self.imap.messages = {12: base64.b64decode(self.payload['raw']), 13: base64.b64decode(self.payload['raw'])}
        result = self.save()
        self.assertEqual(result['copy_count'], 2)
        self.assertEqual(self.imap.append_calls, [])

    def test_exact_header_match_rejects_substring(self):
        self.imap.messages[12] = ('Message-ID: ' + self.payload['message_id'].replace('>', '.extra>') + '\r\n\r\n').encode()
        self.assertEqual(self.check()['status'], 'missing')

    def test_truncated_candidates_cannot_prove_missing(self):
        self.imap.messages = {i: b'Message-ID: <other@example.com>\r\n\r\n' for i in range(1, 22)}
        result = self.save()
        self.assertEqual(result['status'], 'incomplete')
        self.assertTrue(result['truncated'])
        self.assertEqual(self.imap.append_calls, [])

    def test_ambiguous_sent_folder_requires_choice(self):
        self.imap.listing.append(b'(\\Sent) "/" "Other Sent"')
        self.assertEqual(self.save()['status'], 'needs_mailbox')
        self.assertEqual(self.imap.append_calls, [])
        self.assertEqual(self.save('Other Sent')['status'], 'saved')

    def test_missing_special_use_flag_does_not_guess_english_name(self):
        self.imap.listing = [b'() "/" "Sent Items"']
        self.assertEqual(self.save()['status'], 'needs_mailbox')
        self.assertEqual(self.save('Sent Items')['status'], 'saved')

    def test_explicit_nonexistent_folder_not_created(self):
        self.assertEqual(self.save('Missing')['status'], 'needs_mailbox')
        self.assertEqual(self.imap.append_calls, [])

    def test_disconnect_during_append_disables_replay(self):
        self.imap.append_error = OSError('synthetic disconnect')
        self.assertEqual(self.save()['status'], 'unknown')
        self.imap.append_error = None
        self.assertEqual(self.save()['status'], 'previous_attempt')
        self.assertEqual(len(self.imap.append_calls), 1)

    def test_append_rejected_not_automatically_retried(self):
        self.imap.append_code = 'NO'
        self.assertEqual(self.save()['status'], 'rejected')
        self.imap.append_code = 'OK'
        self.assertEqual(self.save()['status'], 'previous_attempt')
        self.assertEqual(len(self.imap.append_calls), 1)

    def test_gate_persistence_failure_stops_before_append(self):
        with patch.object(self.store, 'claim_copy', side_effect=MailError('synthetic failure')):
            with self.assertRaises(MailError):
                self.save()
        self.assertEqual(self.imap.append_calls, [])

    def test_receipt_failure_after_append_can_be_resolved_read_only(self):
        with patch.object(self.store, 'finish_copy', side_effect=MailError('synthetic failure')):
            self.assertEqual(self.save()['status'], 'saved_unverified')
        self.assertEqual(self.check()['status'], 'present')
        self.assertEqual(self.save()['status'], 'present')
        self.assertEqual(len(self.imap.append_calls), 1)

    def test_missing_after_success_does_not_allow_reappend(self):
        self.imap.hide_after_append = True
        self.assertEqual(self.save()['status'], 'saved_unverified')
        self.assertEqual(self.save()['status'], 'previous_attempt')
        self.assertEqual(len(self.imap.append_calls), 1)

    def test_lookup_failure_after_success_does_not_erase_positive_append(self):
        self.imap.fail_after_append = True
        self.assertEqual(self.save()['status'], 'saved_unverified')
        self.assertEqual(self.store.copy_attempt(self.identifier)['state'], 'saved')

    def test_multiple_connections_can_claim_only_once(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.store.claim_copy(self.identifier, 'Sent Items'), range(12)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.save()['status'], 'previous_attempt')
        self.assertEqual(self.imap.append_calls, [])

    def test_changed_account_rejected_before_network(self):
        other = MailClient(MailConfig('other@mail.ustc.edu.cn'), 'synthetic-password')
        with self.assertRaises(MailError):
            sent.save(other, self.identifier, store=self.store)
        self.assertEqual(self.imap.calls, [])

    def test_resume_uses_original_explicit_folder(self):
        self.imap.listing.append(b'() "/" "Chosen Sent"')
        self.save('Chosen Sent')
        self.imap.calls.clear()
        self.check()
        self.assertTrue(any(c[0] == 'SELECT' and c[1] == '"Chosen Sent"' for c in self.imap.calls))


if __name__ == '__main__':
    unittest.main()
