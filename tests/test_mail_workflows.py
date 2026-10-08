import base64
import json
import os
import tempfile
import unittest
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import Mock, patch

from school_mcp.mail import actions, classification, compose_extra, exporting, outbox, threads
from school_mcp.mail.client import MailClient
from school_mcp.mail.config import MailConfig, MailError
from school_mcp.mail.selection import references
from mail_workflow_support import MemoryIMAP, message


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.server = MemoryIMAP()
        self.client = MailClient(MailConfig('student@mail.ustc.edu.cn'), 'synthetic-password')
        self.ref = {'mailbox': 'INBOX', 'uid': 1, 'uid_validity': 100}
        self.plans = actions.Plans(Path(self.directory.name))
        self.rules = classification.ClassificationStore(Path(self.directory.name))
        self.outbox = outbox.Outbox(Path(self.directory.name))
        for p in (patch('school_mcp.mail.client.imaplib.IMAP4_SSL', return_value=self.server),
                  patch('school_mcp.network.limiter', Mock()),
                  patch('school_mcp.mail.outbox._dpapi', side_effect=lambda data, decrypt=False: base64.b64decode(data) if decrypt else base64.b64encode(data)),
                  patch.dict(os.environ, {'SCHOOL_MCP_LOCAL_DIR': self.directory.name})):
            p.start()
            self.addCleanup(p.stop)

    def prepare(self, action='archive', **kwargs):
        return actions.prepare(self.client, [{**self.ref, 'action': action, **kwargs}], store=self.plans)

    def execute(self, plan):
        return actions.execute(self.client, plan['plan_id'], plan['plan_sha256'], store=self.plans)

    def test_archive_and_undo_preserve_bytes_unread_and_other_deleted(self):
        raw = self.server.mailboxes['INBOX'][1][0]
        self.server.mailboxes['INBOX'][2] = (b'other deleted message', {'\\deleted'})
        plan = self.prepare(categories=['课程'])
        self.assertFalse(any(c[0] in {'COPY', 'STORE', 'EXPUNGE'} for c in self.server.calls))
        done = self.execute(plan)
        self.assertEqual(done['status'], 'completed')
        self.assertNotIn(1, self.server.mailboxes['INBOX'])
        self.assertIn(2, self.server.mailboxes['INBOX'])
        self.assertEqual(self.server.mailboxes['归档文件夹/课程'][51], (raw, set()))
        undo = actions.prepare_undo(self.client, plan['plan_id'], store=self.plans)
        self.assertEqual(self.execute(undo)['status'], 'completed')
        self.assertEqual(self.server.mailboxes['INBOX'][52], (raw, set()))
        self.assertFalse(self.server.mailboxes['归档文件夹/课程'])

    def test_repeat_execution_does_not_duplicate_copy(self):
        plan = self.prepare()
        self.execute(plan)
        self.execute(plan)
        self.assertEqual(sum(c[0] == 'COPY' for c in self.server.calls), 1)

    def test_move_outside_archive_not_archived(self):
        plan = self.prepare('move', target='Other')
        self.assertFalse(plan['items'][0]['is_archive'])
        self.assertEqual(self.execute(plan)['status'], 'completed')

    def test_lookalike_prefix_not_archive(self):
        self.assertFalse(actions.is_archive('归档文件夹Old', self.client.folders()['folders']))

    def test_mark_read_then_undo(self):
        plan = self.prepare('mark_read')
        self.assertEqual(self.execute(plan)['status'], 'completed')
        self.assertIn('\\seen', self.server.mailboxes['INBOX'][1][1])
        undo = actions.prepare_undo(self.client, plan['plan_id'], store=self.plans)
        self.execute(undo)
        self.assertNotIn('\\seen', self.server.mailboxes['INBOX'][1][1])
        self.assertFalse(any(c[0] in {'COPY', 'EXPUNGE'} for c in self.server.calls))

    def test_undo_refuses_later_change(self):
        plan = self.prepare('mark_read')
        self.execute(plan)
        self.server.mailboxes['INBOX'][1][1].add('\\flagged')
        with self.assertRaises(MailError): actions.prepare_undo(self.client, plan['plan_id'], store=self.plans)

    def test_prepare_undo_reuses_identifier(self):
        plan = self.prepare('mark_read')
        self.execute(plan)
        first = actions.prepare_undo(self.client, plan['plan_id'], store=self.plans)
        second = actions.prepare_undo(self.client, plan['plan_id'], store=self.plans)
        self.assertEqual(first['plan_id'], second['plan_id'])

    def test_undo_refuses_recreated_original_folder(self):
        plan = self.prepare()
        self.execute(plan)
        self.server.validities['INBOX'] = 999
        with self.assertRaises(MailError): actions.prepare_undo(self.client, plan['plan_id'], store=self.plans)

    def test_competing_undo_links_disable_losing_candidate(self):
        original = self.prepare('mark_read')
        self.execute(original)
        first, second = self.prepare('mark_unread'), self.prepare('mark_unread')
        self.assertEqual(self.plans.attach_undo(original['plan_id'], first['plan_id']), first['plan_id'])
        self.assertEqual(self.plans.attach_undo(original['plan_id'], second['plan_id']), first['plan_id'])
        self.assertEqual(self.plans.get_plan(second['plan_id'])[0], 'conflict')

    def test_archive_child_counts_when_server_omits_container_from_list(self):
        self.assertTrue(actions.is_archive('归档文件夹/课程', [{'name': '归档文件夹/课程', 'delimiter': '/'}]))

    def test_conflict_stops_item_before_mutation(self):
        plan = self.prepare()
        self.server.mailboxes['INBOX'][1][1].add('\\seen')
        self.assertEqual(self.execute(plan)['items'][0]['status'], 'conflict')
        self.assertFalse(any(c[0] == 'COPY' for c in self.server.calls))

    def test_uidvalidity_changed_stops(self):
        plan = self.prepare()
        self.server.validities['INBOX'] = 999
        self.assertEqual(self.execute(plan)['status'], 'review')
        self.assertFalse(any(c[0] == 'COPY' for c in self.server.calls))

    def test_target_uidvalidity_changed_stops_before_copy(self):
        plan = self.prepare()
        self.server.validities['归档文件夹'] = 999
        self.assertEqual(self.execute(plan)['status'], 'review')
        self.assertFalse(any(c[0] == 'COPY' for c in self.server.calls))

    def test_partial_plan_retains_success_and_reports_conflict(self):
        self.server.mailboxes['INBOX'][2] = (message(identifier='<two@example.edu>').as_bytes(), set())
        plan = actions.prepare(self.client, [{**self.ref, 'action': 'mark_read'},
            {**self.ref, 'uid': 2, 'action': 'mark_read'}], store=self.plans)
        self.server.mailboxes['INBOX'][2][1].add('\\flagged')
        result = self.execute(plan)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual([i['status'] for i in result['items']], ['completed', 'conflict'])
        undo = actions.prepare_undo(self.client, plan['plan_id'], store=self.plans)
        self.assertEqual(len(undo['items']), 1)

    def test_copy_hash_mismatch_preserves_source(self):
        original_uid = self.server.uid
        def uid(command, *args):
            result = original_uid(command, *args)
            if command == 'COPY':
                self.server.mailboxes['归档文件夹'][51] = (message(body='changed').as_bytes(), set())
            return result
        with patch.object(self.server, 'uid', side_effect=uid):
            self.assertEqual(self.execute(self.prepare())['status'], 'review')
        self.assertIn(1, self.server.mailboxes['INBOX'])
        self.assertFalse(any(c[0] in {'STORE', 'EXPUNGE'} for c in self.server.calls))

    def test_claim_is_cross_instance_single_attempt(self):
        plan = self.prepare()
        _, payload = self.plans.get_plan(plan['plan_id'])
        other = actions.Plans(Path(self.directory.name))
        self.assertTrue(self.plans.claim_plan(plan['plan_id'], payload))
        self.assertFalse(other.claim_plan(plan['plan_id'], payload))
        self.assertEqual(self.execute(plan)['status'], 'running')

    def test_missing_flags_fails_before_any_write(self):
        original_uid = self.server.uid
        def uid(command, *args):
            result = original_uid(command, *args)
            if command == 'FETCH' and 'BODY.PEEK[]' in args[-1]:
                return 'OK', [(b'1 (UID 1)', result[1][0][1])]
            return result
        with patch.object(self.server, 'uid', side_effect=uid):
            with self.assertRaises(MailError): self.prepare()

    def test_missing_uidplus_never_uses_global_expunge(self):
        self.server.supports_uidplus = False
        self.assertEqual(self.execute(self.prepare())['status'], 'review')
        self.assertFalse(any(c[0] == 'COPY' for c in self.server.calls))

    def test_lost_copy_response_keeps_source_and_blocks_new_plan(self):
        self.server.fail_after = 'COPY'
        plan = self.prepare()
        done = self.execute(plan)
        self.assertEqual(done['status'], 'review')
        self.assertIn(1, self.server.mailboxes['INBOX'])
        self.assertIn(51, self.server.mailboxes['归档文件夹'])
        self.execute(plan)
        other = self.prepare()
        with self.assertRaises(MailError): self.execute(other)
        self.assertEqual(sum(c[0] == 'COPY' for c in self.server.calls), 1)

    def test_wrong_copyuid_never_removes_source(self):
        self.server.copy_mapping_override = b'101 2 51'
        self.assertEqual(self.execute(self.prepare())['status'], 'review')
        self.assertFalse(any(c[0] in {'STORE', 'EXPUNGE'} for c in self.server.calls))

    def test_lost_expunge_response_reconciles_without_retry(self):
        self.server.fail_after = 'EXPUNGE'
        plan = self.prepare()
        done = self.execute(plan)
        self.assertEqual(done['status'], 'review')
        check = actions.status(self.client, plan['plan_id'], True, store=self.plans)
        self.assertFalse(check['observations'][0]['before']['present'])
        self.assertTrue(check['observations'][0]['after']['matches_snapshot'])
        self.assertEqual(check['status'], 'review')
        self.execute(plan)
        self.assertEqual(sum(c[0] == 'EXPUNGE' for c in self.server.calls), 1)

    def test_duplicate_refs_invalid(self):
        with self.assertRaises(MailError): references([self.ref, self.ref])

    def test_digest_and_account_guard(self):
        plan = self.prepare()
        with self.assertRaises(MailError): actions.execute(self.client, plan['plan_id'], 'bad', store=self.plans)
        other = MailClient(MailConfig('other@mail.ustc.edu.cn'), 'synthetic')
        with self.assertRaises(MailError): actions.status(other, plan['plan_id'], store=self.plans)

    def test_create_uses_archive_root_idempotently(self):
        result = actions.create_folder(self.client, archive=True, categories=['活动'])
        self.assertEqual(result['folder']['name'], '归档文件夹/活动')
        self.assertEqual(actions.create_folder(self.client, archive=True, categories=['活动'])['status'], 'exists')
        self.assertEqual(sum(c[0] == 'CREATE' for c in self.server.calls), 1)

    def test_forward_retains_attachment_bytes_and_full_body(self):
        original = message(body='完整正文' * 2000)
        original.add_attachment(b'original bytes', maintype='application', subtype='octet-stream', filename='test.bin')
        self.server.mailboxes['INBOX'][1] = (original.as_bytes(), set())
        draft = compose_extra.forward(self.client, self.ref, ['recipient@example.com'], store=self.outbox)
        _, payload = self.outbox.get(draft['draft_id'])
        parsed = BytesParser(policy=policy.default).parsebytes(base64.b64decode(payload['raw']))
        self.assertIn('完整正文' * 2000, parsed.get_body(preferencelist=('plain',)).get_content())
        self.assertEqual(next(parsed.iter_attachments()).get_payload(decode=True), b'original bytes')
        self.assertFalse(self.server.mailboxes['INBOX'][1][1])

    def test_forward_can_explicitly_omit_attachments(self):
        original = message()
        original.add_attachment(b'data', maintype='application', subtype='octet-stream', filename='x')
        self.server.mailboxes['INBOX'][1] = (original.as_bytes(), set())
        draft = compose_extra.forward(self.client, self.ref, ['recipient@example.com'], include_attachments=False, store=self.outbox)
        self.assertEqual(draft['preview']['attachments'], [])

    def test_forward_too_large_body_needs_input_not_truncated(self):
        self.server.mailboxes['INBOX'][1] = (message(body='X' * 100001).as_bytes(), set())
        result = compose_extra.forward(self.client, self.ref, ['recipient@example.com'], store=self.outbox)
        self.assertEqual(result['status'], 'needs_input')

    def test_reply_all_deduplicates_aliases_cc_and_no_bcc(self):
        original = message()
        original['Reply-To'] = 'reply@example.edu'
        original.replace_header('To', 'student@mail.ustc.edu.cn, other@example.com, REPLY@example.edu')
        original['Cc'] = 'other@example.com, copy@example.com, alias@example.com'
        original['Bcc'] = 'hidden@example.com'
        self.server.mailboxes['INBOX'][1] = (original.as_bytes(), set())
        draft = compose_extra.reply_all(self.client, self.ref, '回复内容', ['alias@example.com'], store=self.outbox)
        self.assertEqual(draft['preview']['to'], ['reply@example.edu', 'other@example.com'])
        self.assertEqual(draft['preview']['cc'], ['copy@example.com'])
        self.assertEqual(draft['preview']['bcc'], [])
        _, payload = self.outbox.get(draft['draft_id'])
        parsed = BytesParser(policy=policy.default).parsebytes(base64.b64decode(payload['raw']))
        self.assertEqual(str(parsed['In-Reply-To']), '<one@example.edu>')
        self.assertIsNone(parsed['Bcc'])

    def test_reply_all_cc_only_keeps_cc_role(self):
        original = message(sender=self.client.config.address)
        original['Cc'] = 'copy@example.com'
        self.server.mailboxes['INBOX'][1] = (original.as_bytes(), set())
        draft = compose_extra.reply_all(self.client, self.ref, 'reply', store=self.outbox)
        self.assertEqual(draft['status'], 'ready')
        self.assertEqual(draft['preview']['to'], [])
        self.assertEqual(draft['preview']['cc'], ['copy@example.com'])

    def test_forward_combined_attachment_limit_does_not_drop_any(self):
        original = message()
        for index in range(10):
            original.add_attachment(b'x', maintype='application', subtype='octet-stream', filename=str(index))
        self.server.mailboxes['INBOX'][1] = (original.as_bytes(), set())
        extra = Path(self.directory.name) / 'extra.txt'
        extra.write_bytes(b'x')
        draft = compose_extra.forward(self.client, self.ref, ['recipient@example.com'], attachments=[str(extra)], store=self.outbox)
        self.assertEqual(draft['status'], 'needs_input')

    def test_nested_message_attachment_remains_eml_without_duplicate_nested_files(self):
        original = message()
        nested = message(identifier='<nested@example.edu>')
        nested.add_attachment(b'nested', maintype='application', subtype='octet-stream', filename='nested.bin')
        original.add_attachment(nested, filename='original.eml')
        self.server.mailboxes['INBOX'][1] = (original.as_bytes(), set())
        draft = compose_extra.forward(self.client, self.ref, ['recipient@example.com'], store=self.outbox)
        self.assertEqual(len(draft['preview']['attachments']), 1)
        _, payload = self.outbox.get(draft['draft_id'])
        parsed = BytesParser(policy=policy.default).parsebytes(base64.b64decode(payload['raw']))
        attachment = next(parsed.iter_attachments())
        nested_parsed = BytesParser(policy=policy.default).parsebytes(attachment.get_payload(decode=True))
        self.assertEqual(next(nested_parsed.iter_attachments()).get_payload(decode=True), b'nested')

    def test_eml_export_exact_bytes_and_zip_manifest(self):
        original = self.server.mailboxes['INBOX'][1][0]
        result = exporting.export(self.client, [self.ref])
        self.assertEqual(Path(result['path']).read_bytes(), original)
        self.server.mailboxes['INBOX'][2] = (message(identifier='<two@example.edu>').as_bytes(), set())
        result = exporting.export(self.client, [self.ref, {**self.ref, 'uid': 2}])
        with zipfile.ZipFile(result['path']) as archive:
            self.assertEqual(len(json.loads(archive.read('manifest.json'))), 2)
            self.assertEqual(archive.read('01-message-1.eml'), original)

    def test_markdown_export_escapes_remote_images(self):
        self.server.mailboxes['INBOX'][1] = (message(body='![image](https://example.com/a.png)<img src=x>').as_bytes(), set())
        result = exporting.export(self.client, [self.ref], 'md')
        text = Path(result['path']).read_text(encoding='utf-8')
        self.assertIn('\\!\\[image\\]', text)
        self.assertIn('\\<img', text)

    def config(self, rules=None):
        return classification.save_rules(self.client, ['课程', '活动'], rules or [
            {'category': '课程', 'field': 'domain', 'value': 'example.edu', 'priority': 100}], store=self.rules)

    def preview(self):
        return classification.preview(self.client, [self.ref], store=self.rules)

    def classification_plan(self, preview, suggestions=None):
        return classification.prepare(self.client, preview['preview_id'], preview['preview_sha256'], suggestions, store=self.rules, plans=self.plans)

    def test_classification_requires_categories(self):
        self.assertEqual(self.preview()['status'], 'needs_categories')

    def test_rule_classification_through_archive_plan(self):
        self.config()
        preview = self.preview()
        self.assertEqual(preview['items'][0]['decision'], 'rule')
        plan = self.classification_plan(preview)
        self.assertEqual(plan['items'][0]['target'], '归档文件夹/课程')
        self.assertEqual(self.execute(plan)['status'], 'completed')

    def test_authentication_mail_is_protected_even_when_read(self):
        self.config()
        self.server.mailboxes['INBOX'][1] = (message(body='本次验证码为示例。').as_bytes(), {'\\seen'})
        preview = self.preview()
        self.assertEqual(preview['items'][0]['decision'], 'protected')
        self.assertEqual(self.classification_plan(preview)['status'], 'no_changes')
        with self.assertRaises(MailError): self.classification_plan(preview, [{**self.ref, 'category': '课程', 'reason': 'override'}])

    def test_conflicting_rules_keep_original(self):
        self.config([{'category': c, 'field': 'domain', 'value': 'example.edu'} for c in ['课程', '活动']])
        preview = self.preview()
        self.assertEqual(preview['items'][0]['decision'], 'conflict')
        self.assertEqual(self.classification_plan(preview)['status'], 'no_changes')

    def test_priority_is_deterministic(self):
        self.config([{'category': '课程', 'field': 'domain', 'value': 'example.edu', 'priority': 2},
                     {'category': '活动', 'field': 'subject_contains', 'value': '通知', 'priority': 1}])
        self.assertEqual(self.preview()['items'][0]['category'], '活动')

    def test_unmatched_can_have_reasoned_existing_category_suggestion(self):
        self.config([{'category': '课程', 'field': 'subject_contains', 'value': '不存在'}])
        preview = self.preview()
        self.assertEqual(preview['items'][0]['decision'], 'unmatched')
        self.assertEqual(self.classification_plan(preview)['status'], 'no_changes')
        plan = self.classification_plan(preview, [{**self.ref, 'category': '课程', 'reason': '主题说明课程安排'}])
        self.assertEqual(plan['status'], 'ready')

    def test_classification_stale_rules_and_message_are_rejected(self):
        self.config()
        preview = self.preview()
        self.server.mailboxes['INBOX'][1][1].add('\\seen')
        with self.assertRaises(MailError): self.classification_plan(preview)
        preview = self.preview()
        self.config()
        with self.assertRaises(MailError): self.classification_plan(preview)

    def test_thread_does_not_merge_same_subject_without_references(self):
        reply = message(identifier='<reply@example.edu>', body='回复内容')
        reply['In-Reply-To'], reply['References'] = '<one@example.edu>', '<one@example.edu>'
        self.server.mailboxes['INBOX'][2] = (reply.as_bytes(), set())
        self.server.mailboxes['INBOX'][3] = (message(identifier='<unrelated@example.edu>').as_bytes(), set())
        result = threads.read_thread(self.client, self.ref)
        self.assertEqual({v['uid'] for v in result['messages']}, {1, 2})
        self.assertTrue(all(not flags for _, flags in self.server.mailboxes['INBOX'].values()))

    def test_thread_limit_explicitly_reports_incomplete(self):
        result = threads.read_thread(self.client, self.ref, limit=1)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['messages']), 1)


if __name__ == '__main__': unittest.main()
