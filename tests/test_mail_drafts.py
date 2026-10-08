from __future__ import annotations

import base64
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from school_mcp.mail import outbox
from school_mcp.mail.config import MailConfig, MailError
from school_mcp.network import CooldownError
from test_mail_outbox import FakeSMTP


class DraftLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = outbox.Outbox(self.root)
        self.config = MailConfig('student@mail.ustc.edu.cn')
        self.smtp = FakeSMTP()
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
        fields = dict(to=['recipient@example.com'], subject='synthetic draft', body='synthetic original')
        fields.update(kwargs)
        return outbox.prepare(self.config, store=self.store, **fields)

    def update(self, draft, **kwargs):
        return outbox.update(self.config, draft['draft_id'], draft['content_sha256'], store=self.store, **kwargs)

    def cancel(self, draft):
        return outbox.cancel(self.config, draft['draft_id'], draft['content_sha256'], store=self.store)

    def send(self, draft):
        return outbox.send(self.config, draft['draft_id'], draft['content_sha256'], store=self.store)

    def all(self):
        return outbox.list_drafts(self.config, state='all', store=self.store)['drafts']

    def test_update_creates_new_id_and_disables_old_send(self):
        original = self.prepare()
        revised = self.update(original, to=['other@example.com'], subject='Changed', body='Changed body', bcc=['hidden@example.com'])
        self.assertTrue(revised['mutation_applied'])
        self.assertEqual(revised['revision'], 2)
        self.assertEqual(revised['previous_draft_id'], original['draft_id'])
        self.assertNotEqual(revised['message_id'], original['message_id'])
        self.assertNotEqual(revised['content_sha256'], original['content_sha256'])
        old = self.send(original)
        self.assertEqual(old['status'], 'superseded')
        self.assertEqual(old['superseded_by'], revised['draft_id'])
        self.connector.assert_not_called()
        self.assertEqual(self.send(revised)['status'], 'accepted')
        msg = BytesParser(policy=policy.default).parsebytes(self.smtp.data.call_args.args[0])
        self.assertEqual(msg['To'], 'other@example.com')
        self.assertIsNone(msg['Bcc'])

    def test_edit_retry_returns_successor_without_creating_more(self):
        original = self.prepare()
        revised = self.update(original, body='Changed')
        retried = self.update(original, body='Another change')
        self.assertFalse(retried['mutation_applied'])
        self.assertEqual(retried['superseded_by'], revised['draft_id'])
        self.assertEqual(len(self.all()), 2)

    def test_chain_versions_remain_traceable(self):
        first = self.prepare()
        second = self.update(first, body='Second')
        third = self.update(second, body='Third')
        self.assertEqual(third['revision'], 3)
        self.assertEqual(third['previous_draft_id'], second['draft_id'])
        self.assertEqual([x['status'] for x in self.all()], ['ready', 'superseded', 'superseded'])

    def test_omitted_attachments_keep_bytes_after_original_deleted(self):
        path = self.root / 'synthetic.txt'
        path.write_bytes(b'original bytes')
        original = self.prepare(attachments=[str(path)])
        path.unlink()
        revised = self.update(original, body='Changed')
        self.assertEqual(revised['preview']['attachments'], original['preview']['attachments'])
        _, payload = self.store.get(revised['draft_id'])
        msg = BytesParser(policy=policy.default).parsebytes(base64.b64decode(payload['raw']))
        self.assertEqual(next(msg.iter_attachments()).get_payload(decode=True), b'original bytes')

    def test_explicit_empty_arrays_clear_cc_bcc_and_attachments(self):
        path = self.root / 'synthetic.txt'
        path.write_bytes(b'original bytes')
        original = self.prepare(cc=['cc@example.com'], bcc=['hidden@example.com'], attachments=[str(path)])
        revised = self.update(original, cc=[], bcc=[], attachments=[])
        self.assertEqual(revised['preview']['cc'], [])
        self.assertEqual(revised['preview']['bcc'], [])
        self.assertEqual(revised['preview']['attachments'], [])

    def test_attachment_array_replaces_whole_set(self):
        first, second = self.root / 'first.txt', self.root / 'second.txt'
        first.write_bytes(b'first')
        second.write_bytes(b'second')
        original = self.prepare(attachments=[str(first)])
        revised = self.update(original, attachments=[str(second)])
        self.assertEqual([x['filename'] for x in revised['preview']['attachments']], ['second.txt'])

    def test_reply_thread_is_retained(self):
        original = self.prepare(reply_headers={'In-Reply-To': '<synthetic-parent@example.com>', 'References': '<synthetic-parent@example.com>'})
        revised = self.update(original, body='Changed')
        self.assertEqual(revised['preview']['reply_to_message_id'], '<synthetic-parent@example.com>')
        _, payload = self.store.get(revised['draft_id'])
        msg = BytesParser(policy=policy.default).parsebytes(base64.b64decode(payload['raw']))
        self.assertEqual(msg['References'], '<synthetic-parent@example.com>')

    def test_invalid_changes_leave_original_ready(self):
        original = self.prepare()
        for change in ({'to': []}, {'body': ''}, {'subject': 'bad\r\nheader'}, {'attachments': ['missing.txt']}):
            result = self.update(original, **change)
            self.assertEqual(result['status'], 'needs_input')
            self.assertFalse(result['mutation_applied'])
            self.assertEqual(result['next_action'], 'correct_fields_then_update_same_draft')
        self.assertEqual(self.store.get(original['draft_id'])[0], 'ready')
        self.assertEqual(len(self.all()), 1)

    def test_no_change_request_returns_same_draft(self):
        original = self.prepare()
        result = self.update(original)
        self.assertEqual(result['draft_id'], original['draft_id'])
        self.assertFalse(result['mutation_applied'])
        self.assertEqual(len(self.all()), 1)

    def test_cancel_preserves_content_and_stops_send_and_update(self):
        original = self.prepare()
        cancelled = self.cancel(original)
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertTrue(cancelled['mutation_applied'])
        repeated = self.cancel(original)
        self.assertFalse(repeated['mutation_applied'])
        self.assertEqual(cancelled['cancelled_at'], repeated['cancelled_at'])
        self.assertEqual(self.send(original)['status'], 'cancelled')
        self.assertEqual(self.update(original, body='Changed')['status'], 'cancelled')
        preview = outbox.status(original['draft_id'], include_preview=True, store=self.store)['preview']
        self.assertEqual(preview['body'], original['preview']['body'])
        self.connector.assert_not_called()

    def test_cancel_old_version_does_not_cancel_successor(self):
        original = self.prepare()
        revised = self.update(original, body='Changed')
        result = self.cancel(original)
        self.assertFalse(result['mutation_applied'])
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(self.store.get(revised['draft_id'])[0], 'ready')

    def test_no_edit_or_cancel_after_send_claim_or_outcome(self):
        original = self.prepare()
        _, payload = self.store.get(original['draft_id'])
        for state in ('sending', 'unknown', 'accepted', 'partial', 'failed_before_data', 'rejected'):
            self.store.finish(original['draft_id'], state, payload)
            self.assertFalse(self.update(original, body='Changed')['mutation_applied'])
            self.assertFalse(self.cancel(original)['mutation_applied'])
            self.assertEqual(self.store.get(original['draft_id'])[0], state)

    def test_wrong_account_and_digest_stop_mutation(self):
        original = self.prepare()
        for method in (outbox.update, outbox.cancel):
            with self.assertRaises(MailError):
                method(MailConfig('other@mail.ustc.edu.cn'), original['draft_id'], original['content_sha256'], store=self.store)
            with self.assertRaises(MailError):
                method(self.config, original['draft_id'], 'incorrect', store=self.store)
        self.assertEqual(self.store.get(original['draft_id'])[0], 'ready')

    def test_insert_failure_rolls_back_supersede(self):
        original = self.prepare()
        with self.store.database() as db:
            db.execute("CREATE TRIGGER fail_revision BEFORE INSERT ON drafts BEGIN SELECT RAISE(ABORT, 'synthetic insert failure'); END")
        with self.assertRaises(MailError):
            self.update(original, body='Changed')
        self.assertEqual(self.store.get(original['draft_id'])[0], 'ready')
        self.assertEqual(len(self.all()), 1)
        self.assertNotIn('superseded_by', self.store.get(original['draft_id'])[1])

    def test_encryption_failure_leaves_original_ready(self):
        original = self.prepare()
        with patch.object(self.store, 'encode', side_effect=MailError('synthetic encryption failure')):
            with self.assertRaises(MailError):
                self.cancel(original)
            with self.assertRaises(MailError):
                self.update(original, body='Changed')
        self.assertEqual(self.store.get(original['draft_id'])[0], 'ready')

    def race(self, first_name, first_action, second_name, second_action):
        barrier = threading.Barrier(2)
        first_method, second_method = getattr(self.store, first_name), getattr(self.store, second_name)
        def first(*args):
            barrier.wait(timeout=5)
            return first_method(*args)
        def second(*args):
            barrier.wait(timeout=5)
            return second_method(*args)
        with patch.object(self.store, first_name, side_effect=first), patch.object(self.store, second_name, side_effect=second):
            with ThreadPoolExecutor(max_workers=2) as pool:
                a, b = pool.submit(first_action), pool.submit(second_action)
                return a.result(timeout=10), b.result(timeout=10)

    def test_send_and_edit_race_have_only_one_winner(self):
        original = self.prepare()
        edited, sent = self.race('replace_ready', lambda: self.update(original, body='Changed'), 'claim', lambda: self.send(original))
        if edited['mutation_applied']:
            self.assertEqual(sent['status'], 'superseded')
            self.connector.assert_not_called()
        else:
            self.assertEqual(sent['status'], 'accepted')
            self.assertEqual(len(self.all()), 1)
            self.smtp.data.assert_called_once()

    def test_send_and_cancel_race_cannot_claim_false_cancellation(self):
        original = self.prepare()
        cancelled, sent = self.race('cancel_ready', lambda: self.cancel(original), 'claim', lambda: self.send(original))
        if cancelled['mutation_applied']:
            self.assertEqual(sent['status'], 'cancelled')
            self.connector.assert_not_called()
        else:
            self.assertEqual(sent['status'], 'accepted')
            self.assertNotEqual(cancelled['status'], 'cancelled')

    def test_edit_and_cancel_race_have_only_one_winner(self):
        original = self.prepare()
        revised, cancelled = self.race('replace_ready', lambda: self.update(original, body='Changed'), 'cancel_ready', lambda: self.cancel(original))
        self.assertEqual(sum(x['mutation_applied'] for x in (revised, cancelled)), 1)
        self.assertEqual(len(self.all()), 2 if revised['mutation_applied'] else 1)

    def test_concurrent_edits_create_exactly_one_revision(self):
        original = self.prepare()
        barrier = threading.Barrier(2)
        method = self.store.replace_ready
        def replace(*args):
            barrier.wait(timeout=5)
            return method(*args)
        with patch.object(self.store, 'replace_ready', side_effect=replace):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.update, original, body=text) for text in ('First edit', 'Second edit')]
                results = [f.result(timeout=10) for f in futures]
        self.assertEqual(sum(x['mutation_applied'] for x in results), 1)
        self.assertEqual(len(self.all()), 2)

    def test_cancel_during_cooldown_reports_current_state(self):
        original = self.prepare()
        def cooldown(service):
            self.cancel(original)
            raise CooldownError(30)
        self.limiter.acquire.side_effect = cooldown
        result = self.send(original)
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse(result['can_send'])
        self.connector.assert_not_called()

    def test_list_default_minimizes_content_and_filters_account(self):
        first = self.prepare()
        cancelled = self.prepare()
        self.cancel(cancelled)
        outbox.prepare(MailConfig('other@mail.ustc.edu.cn'), to=['other@example.com'], subject='Other account', body='Other body', store=self.store)
        result = outbox.list_drafts(self.config, store=self.store)
        self.assertEqual([x['draft_id'] for x in result['drafts']], [first['draft_id']])
        self.assertNotIn('recipient@example.com', str(result))
        self.assertNotIn('synthetic original', str(result))
        detail = outbox.list_drafts(self.config, include_recipients=True, store=self.store)
        self.assertEqual(detail['drafts'][0]['to'], ['recipient@example.com'])

    def test_pagination_stable_when_new_draft_inserted(self):
        drafts = [self.prepare(subject=str(i)) for i in range(3)]
        first = outbox.list_drafts(self.config, limit=1, store=self.store)
        self.prepare(subject='newer')
        second = outbox.list_drafts(self.config, limit=2, cursor=first['next_cursor'], store=self.store)
        self.assertEqual([x['draft_id'] for x in first['drafts'] + second['drafts']], [x['draft_id'] for x in reversed(drafts)])
        self.assertIsNone(second['next_cursor'])

    def test_bounded_scan_can_return_empty_page_with_next_cursor(self):
        expected = self.prepare()
        _, other = self.store.get(expected['draft_id'])
        other['account'] = 'other@mail.ustc.edu.cn'
        for _ in range(100):
            self.store.create(other)
        page = outbox.list_drafts(self.config, store=self.store)
        self.assertEqual(page['drafts'], [])
        self.assertTrue(page['scan_limited'])
        self.assertEqual(page['next_action'], 'continue_next_cursor')
        next_page = outbox.list_drafts(self.config, cursor=page['next_cursor'], store=self.store)
        self.assertEqual(next_page['drafts'][0]['draft_id'], expected['draft_id'])

    def test_legacy_draft_without_timestamps_is_readable_and_editable(self):
        original = self.prepare()
        _, payload = self.store.get(original['draft_id'])
        payload.pop('created_at')
        payload.pop('revision')
        self.store.finish(original['draft_id'], 'ready', payload)
        listed = outbox.list_drafts(self.config, store=self.store)['drafts'][0]
        self.assertIsNone(listed['created_at'])
        self.assertEqual(self.update(original, body='Changed')['revision'], 2)

    def test_invalid_cursor_and_filter_are_rejected(self):
        for kwargs in ({'state': 'arbitrary'}, {'cursor': '-1'}, {'cursor': '1 OR 1=1'}, {'cursor': '0'}, {'cursor': '9' * 30}, {'limit': 0}):
            with self.assertRaises(MailError):
                outbox.list_drafts(self.config, store=self.store, **kwargs)


if __name__ == '__main__':
    unittest.main()
