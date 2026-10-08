"""Synthetic workflows only: never contact school servers or send real email."""
import copy
import tempfile
import time
import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import ValidationError
from school_mcp import script_jobs as jobs, script_workflows as flows
from school_mcp.workflow_store import WorkflowError
from school_mcp.jw.planner import Constraints


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = jobs.JobStore(Path(self.tmp.name))
        self.account = patch.object(jobs, 'account', return_value='synthetic-account')
        self.account.start()
        self.addCleanup(self.account.stop)
        self.start = jobs.now() - timedelta(seconds=1)

    def create(self, kind='mail_watch', parameters=None):
        return self.store.create(kind.split('_')[0], kind, parameters or {}, self.start.isoformat(),
                                 (self.start + timedelta(hours=1)).isoformat(), 60, 'synthetic-account')

    def test_encrypted_store_deduplicates_and_single_claim(self):
        identifier = self.create(parameters={'secret_marker': 'synthetic-private-content'})
        self.assertEqual(identifier, self.create(parameters={'secret_marker': 'synthetic-private-content'}))
        self.assertNotIn(b'synthetic-private-content', self.store.path.read_bytes())
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(lambda _: self.store.claim(identifier), range(8)))
        self.assertEqual(sum(claims), 1)

    def test_duplicate_worker_does_not_enter(self):
        identifier = self.create()
        self.store.claim(identifier)
        self.assertTrue(self.store.enter(identifier))
        step = Mock()
        jobs.run(identifier, store=self.store, step=step)
        step.assert_not_called()

    def test_unclaimed_cli_cannot_start(self):
        identifier = self.create()
        step = Mock()
        jobs.run(identifier, store=self.store, step=step)
        step.assert_not_called()

    def test_account_change_stops_before_step(self):
        identifier = self.create()
        self.store.claim(identifier)
        step = Mock()
        with patch.object(jobs, 'account', return_value='different-account'):
            jobs.run(identifier, store=self.store, step=step)
        self.assertEqual(self.store.get(identifier)['result']['outcome'], 'account_changed')
        step.assert_not_called()

    def test_cancel_before_effect(self):
        identifier = self.create()
        self.store.update(identifier, state='running')
        self.store.cancel(identifier)
        with self.assertRaises(WorkflowError):
            self.store.effect(identifier, {'operation': {'plan_id': 'synthetic'}})

    def test_cancel_after_effect_reports_inflight(self):
        identifier = self.create()
        self.store.update(identifier, state='running')
        self.store.effect(identifier, {'operation': {'plan_id': 'synthetic'}})
        result = self.store.cancel(identifier)
        self.assertTrue(result['in_flight_may_complete'])

    def test_exception_after_effect_keeps_operation_and_never_retries(self):
        identifier = self.create()
        self.store.claim(identifier)
        def fail(identifier, job, store):
            store.effect(identifier, {'operation': {'draft_id': 'synthetic'}})
            raise TimeoutError('synthetic timeout')
        step = Mock(side_effect=fail)
        jobs.run(identifier, store=self.store, step=step)
        result = self.store.get(identifier)
        self.assertEqual(result['state'], 'review')
        self.assertEqual(result['checkpoint']['operation']['draft_id'], 'synthetic')
        jobs.run(identifier, store=self.store, step=step)
        self.assertEqual(step.call_count, 1)

    def test_read_retry_is_bounded_and_backed_off(self):
        identifier = self.create()
        self.store.claim(identifier)
        elapsed = [jobs.now()]
        waits = []
        def sleep(seconds):
            waits.append(seconds)
            elapsed[0] += timedelta(seconds=seconds)
        step = Mock(side_effect=TimeoutError('synthetic'))
        jobs.run(identifier, store=self.store, step=step, sleep=sleep, clock=lambda: elapsed[0])
        self.assertEqual(step.call_count, 3)
        self.assertEqual(sum(waits), 360)
        self.assertEqual(self.store.get(identifier)['state'], 'review')

    def test_precondition_does_not_retry_or_expose_exception(self):
        identifier = self.create()
        self.store.claim(identifier)
        jobs.run(identifier, store=self.store, step=Mock(side_effect=ValueError('synthetic-private-error')))
        self.assertNotIn('synthetic-private-error', str(self.store.get(identifier)['result']))

    def test_deadline_before_write(self):
        identifier = self.create()
        self.store.update(identifier, state='running', end_at=(jobs.now()-timedelta(seconds=1)).isoformat())
        with self.assertRaises(WorkflowError):
            self.store.effect(identifier, {})

    def test_duplicate_archive_scope_is_blocked(self):
        self.create('mail_archive', {'mailbox': 'INBOX', 'initial_cursor': 1})
        with self.assertRaises(WorkflowError):
            self.create('mail_archive', {'mailbox': 'INBOX', 'initial_cursor': 2})

    def test_unknown_archive_cannot_be_bypassed_with_new_job(self):
        identifier = self.create('mail_archive', {'mailbox': 'INBOX', 'initial_cursor': 1})
        self.store.update(identifier, state='review', result={'outcome': 'archive_requires_review'})
        with self.assertRaises(WorkflowError):
            self.create('mail_archive', {'mailbox': 'INBOX', 'initial_cursor': 2})

    def test_job_summary_does_not_disclose_parameters_or_receipt(self):
        identifier = self.create(parameters={'private': 'synthetic'})
        self.store.update(identifier, result={'outcome': 'synthetic', 'body': 'synthetic-private'})
        result = jobs.view(identifier, 'mail', store=self.store)
        self.assertNotIn('synthetic-private', str(result))
        self.assertIn('synthetic-private', str(jobs.view(identifier, 'mail', store=self.store, include_results=True)))
        with self.assertRaises(WorkflowError):
            jobs.view(identifier, 'jw', store=self.store)

    def test_timezone_and_rate_are_required(self):
        with self.assertRaises(WorkflowError):
            self.store.create('mail', 'mail_watch', {}, '2026-10-08T14:00:00', '2026-10-08T16:00:00', 60, 'x')
        with self.assertRaises(ValidationError):
            flows.Schedule(start_at='x', end_at='y', poll_seconds=1)
        with self.assertRaises(ValidationError):
            flows.MailScan(kind='mail_watch', arbitrary_url='https://example.com')

    @unittest.skipUnless(os.name == 'nt', 'Windows pythonw background integration')
    def test_real_background_worker_waits_and_cancels_without_credentials(self):
        from school_mcp.login_runtime import running
        start = jobs.now() + timedelta(hours=1)
        identifier = self.store.create('mail', 'mail_watch', {}, start.isoformat(),
                                       (start + timedelta(hours=1)).isoformat(), 60, 'synthetic-account')
        # The launcher exits before assertions; the actual worker must survive it.
        subprocess.run([sys.executable, '-c',
                        'from pathlib import Path; import sys; from school_mcp.script_jobs import launch,JobStore; launch(sys.argv[1],JobStore(Path(sys.argv[2])))',
                        identifier, self.tmp.name], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not self.store.get(identifier).get('entered') and time.monotonic() < deadline:
                time.sleep(.1)
            self.assertTrue(self.store.get(identifier).get('entered'))
            result = jobs.view(identifier, 'mail', store=self.store)
            self.assertTrue(result['worker_running'])
            self.assertFalse(result['uses_ai'])
            self.assertEqual(result['state'], 'waiting')
            self.assertFalse((Path(self.tmp.name) / 'mail.json').exists())
        finally:
            self.store.cancel(identifier)
            pid = self.store.get(identifier)['worker']['pid']
            deadline = time.monotonic() + 10
            while running(pid) and time.monotonic() < deadline:
                time.sleep(.1)
        self.assertFalse(running(pid))


class ScanClient:
    from school_mcp.mail.client import MailClient
    _query = staticmethod(MailClient._query)

    def __init__(self):
        self.ids = [1, 3, 5, 7]
        self.expected = []
        self.args = None

    @contextmanager
    def session(self):
        yield self

    def select(self, connection, mailbox, expected=None):
        self.expected.append(expected)
        if expected != 99:
            raise WorkflowError('UIDVALIDITY changed')
        return 99, len(self.ids)

    def uid(self, command, *args):
        assert command == 'SEARCH'  # No BODY reads or STORE writes in a watch.
        self.args = args
        return 'OK', [' '.join(map(str, self.ids)).encode()]


def scan_parameters():
    return dict(kind='mail_watch', mailbox='INBOX', sender='', subject='', since='', before='',
                initial_cursor=0, uid_validity=99, batch_size=2, max_messages=20)


class WorkflowTests(unittest.TestCase):
    def test_coremail_boundary_uses_status_if_examine_omits_uidnext(self):
        client = ScanClient()
        client.select = Mock(return_value=(99, 4))
        client.response = Mock(return_value=('UIDNEXT', [None]))
        client.status = Mock(return_value=('OK', [b'"INBOX" (UIDVALIDITY 99 UIDNEXT 8)']))
        self.assertEqual(flows.mail_boundary(client, 'INBOX'), (99, 7))
        client.status.assert_called_once_with('"INBOX"', '(UIDVALIDITY UIDNEXT)')

    def test_boundary_rejects_identity_change_during_status(self):
        client = ScanClient()
        client.select = Mock(return_value=(99, 4))
        client.response = Mock(return_value=('UIDNEXT', [None]))
        client.status = Mock(return_value=('OK', [b'"INBOX" (UIDVALIDITY 100 UIDNEXT 8)']))
        with self.assertRaises(WorkflowError):
            flows.mail_boundary(client, 'INBOX')

    def test_coremail_omits_uidnext_in_status_uses_uid_only_search(self):
        client = ScanClient()
        client.select = Mock(return_value=(99, 4))
        client.response = Mock(return_value=('UIDNEXT', [None]))
        client.status = Mock(return_value=('OK', [b'"INBOX" (UIDVALIDITY 99)']))
        self.assertEqual(flows.mail_boundary(client, 'INBOX'), (99, 7))
        self.assertEqual(client.args, ('ALL',))

    def test_uid_cursor_survives_moving_prior_batch(self):
        client = ScanClient()
        p = scan_parameters()
        refs, backlog = flows.scan_uids(client, p, {})
        self.assertEqual([r['uid'] for r in refs], [1, 3])
        self.assertTrue(backlog)
        client.ids = [5, 7]
        refs, backlog = flows.scan_uids(client, p, {'cursor': 3, 'processed': 2})
        self.assertEqual([r['uid'] for r in refs], [5, 7])
        self.assertFalse(backlog)

    def test_reverse_uid_range_never_repeats_old_mail(self):
        refs, _ = flows.scan_uids(ScanClient(), scan_parameters(), {'cursor': 7})
        self.assertEqual(refs, [])

    def test_uidvalidity_change_stops(self):
        with self.assertRaises(WorkflowError):
            flows.scan_uids(ScanClient(), {**scan_parameters(), 'uid_validity': 100}, {})

    def test_scan_limit_and_chinese_query(self):
        client = ScanClient()
        refs, backlog = flows.scan_uids(client, {**scan_parameters(), 'subject': '测试', 'max_messages': 1}, {})
        self.assertEqual(len(refs), 1)
        self.assertEqual(client.args[:2], ('CHARSET', 'UTF-8'))
        self.assertTrue(backlog)

    def test_mail_mutations_require_authorization(self):
        schedule = flows.Schedule(start_at='unused', end_at='unused')
        with patch.object(flows, 'mail_client', return_value=Mock()):
            for request in (flows.MailSend(kind='mail_send', draft_id='a'*32, content_sha256='b'*64),
                            flows.MailScan(kind='mail_archive')):
                with self.assertRaises(WorkflowError):
                    flows.schedule_mail(request, schedule, False)

    def test_history_requires_scope_date(self):
        client = ScanClient()
        with patch.object(flows, 'mail_client', return_value=client), self.assertRaises(WorkflowError):
            flows.schedule_mail(flows.MailScan(kind='mail_watch', include_existing=True),
                                flows.Schedule(start_at='unused', end_at='unused'))

    def test_rules_changed_stops_before_scanning(self):
        from school_mcp.mail import classification
        with patch.object(flows, 'mail_client', return_value=Mock()), patch.object(classification, 'get_rules', return_value={'revision': 'new'}), patch.object(flows, 'scan_uids') as scan:
            with self.assertRaises(WorkflowError):
                flows.step('id', {'kind': 'mail_archive', 'parameters': {'rules_revision': 'old'}, 'checkpoint': {}}, Mock())
            scan.assert_not_called()

    def test_archive_checkpoints_before_write_and_partial_stops(self):
        from school_mcp.mail import classification, actions
        client = Mock()
        store = Mock()
        ref = {'uid': 1, 'uid_validity': 99, 'mailbox': 'INBOX'}
        job = dict(kind='mail_archive', parameters={'rules_revision': 'same', 'max_messages': 10}, checkpoint={})
        def execute(*args):
            store.effect.assert_called_once()
            return {'status': 'partial'}
        with patch.object(flows, 'mail_client', return_value=client), patch.object(classification, 'get_rules', return_value={'revision': 'same'}), patch.object(flows, 'scan_uids', return_value=([ref], False)), patch.object(classification, 'preview', return_value={'preview_id': 'id', 'preview_sha256': 'sha', 'items': [{'decision': 'rule'}]}), patch.object(classification, 'prepare', return_value={'status': 'ready', 'plan_id': 'plan', 'plan_sha256': 'sha'}), patch.object(actions, 'execute', side_effect=execute):
            state, result, checkpoint = flows.step('id', job, store)
        self.assertEqual(state, 'review')
        self.assertNotIn('cursor', checkpoint)
        self.assertEqual(checkpoint['operation']['plan_id'], 'plan')

    def test_protected_and_unmatched_archive_no_write(self):
        from school_mcp.mail import classification, actions
        job = dict(kind='mail_archive', parameters={'rules_revision': 'same', 'max_messages': 1}, checkpoint={})
        with patch.object(flows, 'mail_client', return_value=Mock()), patch.object(classification, 'get_rules', return_value={'revision': 'same'}), patch.object(flows, 'scan_uids', return_value=([{'uid': 1}], False)), patch.object(classification, 'preview', return_value={'preview_id': 'id', 'preview_sha256': 'sha', 'items': [{'decision': 'protected'}]}), patch.object(classification, 'prepare', return_value={'status': 'no_changes'}), patch.object(actions, 'execute') as execute:
            state, result, checkpoint = flows.step('id', job, Mock())
        self.assertEqual(state, 'completed')
        self.assertEqual(result['classification_counts'], {'protected': 1})
        execute.assert_not_called()

    def test_unknown_smtp_does_not_save_copy_or_resend(self):
        from school_mcp.mail import outbox, sent
        client = SimpleNamespace(config=SimpleNamespace(address='synthetic@example.com'))
        p = dict(draft_id='a'*32, content_sha256='b'*64, save_sent_copy=True)
        with patch.object(flows, 'mail_client', return_value=client), patch.object(outbox.Outbox, 'get', return_value=('ready', {'account': client.config.address})), patch.object(outbox, 'send', return_value={'status': 'unknown'}) as send, patch.object(sent, 'save') as save:
            state, result, cp = flows.step('id', dict(kind='mail_send', parameters=p, checkpoint={}), Mock())
        self.assertEqual(state, 'review')
        send.assert_called_once()
        save.assert_not_called()

    def test_accepted_send_saves_copy_but_not_delivery_claim(self):
        from school_mcp.mail import outbox, sent
        client = SimpleNamespace(config=SimpleNamespace(address='synthetic@example.com'))
        p = dict(draft_id='a'*32, content_sha256='b'*64, save_sent_copy=True)
        with patch.object(flows, 'mail_client', return_value=client), patch.object(outbox.Outbox, 'get', return_value=('ready', {'account': client.config.address})), patch.object(outbox, 'send', return_value={'status': 'accepted'}), patch.object(sent, 'save', return_value={'status': 'saved'}):
            state, result, _ = flows.step('id', dict(kind='mail_send', parameters=p, checkpoint={}), Mock())
        self.assertEqual(state, 'completed')
        self.assertFalse(result['delivery_confirmed'])

    def test_reply_tracking_retains_only_references(self):
        from school_mcp.mail import outbox
        client = Mock()
        client.config.address = 'synthetic@example.com'
        client.find_replies.return_value = dict(status='replies_found', messages=[dict(uid=1, uid_validity=2, mailbox='INBOX', subject='synthetic private')], truncated=False, match_basis='thread_headers')
        with patch.object(flows, 'mail_client', return_value=client), patch.object(outbox.Outbox, 'get', return_value=('accepted', dict(account=client.config.address, message_id='<synthetic@example.com>'))):
            state, result, _ = flows.step('id', dict(kind='mail_replies', parameters=dict(draft_id='a'*32, mailbox='INBOX'), checkpoint={}), Mock())
        self.assertEqual(state, 'completed')
        self.assertNotIn('synthetic private', str(result))

    def test_bb_unknown_only_verifies_existing_attempt(self):
        from school_mcp.bb import submissions
        with patch.object(submissions, 'submit_assignment', return_value={'state': 'uncertain'}) as send, patch.object(submissions, 'submission_status', return_value={'state': 'verified'}) as verify:
            state, _, cp = flows.step('id', dict(kind='bb_submit', parameters={'preparation_id': 'plan', 'expected_sha256': 'sha'}, checkpoint={}), Mock())
        self.assertEqual(state, 'completed')
        send.assert_called_once()
        verify.assert_called_once_with('plan', verify=True)
        self.assertEqual(cp['operation']['preparation_id'], 'plan')

    def test_ambiguous_bb_course_never_prepares_submission(self):
        from school_mcp.bb.client import BBClient
        from school_mcp.bb import submissions
        with patch.object(BBClient, 'courses', return_value={'courses': [{'course_id': '1'}, {'course_id': '2'}]}), patch.object(submissions, 'prepare_submission') as prepare:
            result = flows.prepare_bb_by_name('synthetic', 'homework')
        self.assertEqual(result['state'], 'needs_input')
        prepare.assert_not_called()

    def test_enrollment_open_stops_without_mutation(self):
        from school_mcp.jw.academic import AcademicClient
        with patch.object(AcademicClient, 'window', return_value={'state': 'requires_live_contract'}), patch.object(flows, 'find_offering', return_value={'capacity': None, 'selected_count': 3}):
            state, result, _ = flows.step('id', dict(kind='jw_watch', parameters=dict(semester_id=1, keyword='synthetic', lesson_id=9, minimum_seats=1), checkpoint={}), Mock())
        self.assertEqual(state, 'review')
        self.assertIsNone(result['available_seats'])
        self.assertFalse(result['mutation_performed'])

    def test_open_full_course_keeps_watching_for_seats(self):
        from school_mcp.jw.academic import AcademicClient
        with patch.object(AcademicClient, 'window', return_value={'state': 'requires_live_contract'}), patch.object(flows, 'find_offering', return_value={'capacity': 30, 'selected_count': 30}):
            state, result, _ = flows.step('id', dict(kind='jw_watch', parameters=dict(semester_id=1, keyword='synthetic', lesson_id=9, minimum_seats=1), checkpoint={}), Mock())
        self.assertEqual(state, 'waiting')
        self.assertEqual(result['outcome'], 'waiting_for_seats')

    def test_planning_preserves_selected_course_and_no_silent_truncation(self):
        existing = dict(lesson_id='1', course_code='A', course_name='A', semester_id=1, credits=2,
                        schedule_known=True, required=True, source='official',
                        slots=[dict(weekday=1, start_period=1, end_period=2, weeks=[1])])
        optional = copy.deepcopy(existing)
        optional.update(lesson_id='2', course_code='B', course_name='B', required=False)
        optional['slots'][0]['weekday'] = 2
        client = Mock()
        client.planning_context.return_value = dict(state='context_ready', candidates=[existing])
        client.offerings.return_value = dict(truncated=False, next_page=None, offerings=[{'planner_candidate': optional}])
        result = flows.plan_timetable(1, ['synthetic'], Constraints(min_credits=4, max_credits=4), client)
        self.assertEqual(result['plans'][0]['lesson_ids'], ['1', '2'])
        client.offerings.return_value['truncated'] = True
        self.assertEqual(flows.plan_timetable(1, ['synthetic'], Constraints(), client)['state'], 'needs_input')


if __name__ == '__main__':
    unittest.main()
