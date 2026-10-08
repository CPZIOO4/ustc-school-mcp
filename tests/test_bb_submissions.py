import base64
import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from school_mcp.bb import submissions as api
from school_mcp.bb.session import BBError
from school_mcp.bb.submission_contracts import deadline, file_record, snapshot, form_contract
from school_mcp.bb.submission_browser import check_multipart, static_request

A = {'course_id': '_1_1', 'content_id': '_2_1', 'title': '合成作业', 'due_text': [], 'description': 'synthetic',
     'entry_path': '/webapps/assignment/uploadAssignment?course_id=_1_1&content_id=_2_1&mode=view'}
CURRENT = {'kind': 'history', 'attempt_id': '_3_1', 'files': [{'filename': 'original.pdf', 'url': 'synthetic'}],
           'due_text': '2099年1月1日 下午11:59', 'due_at': '2099-01-01T23:59:00+08:00',
           'new_url': 'synthetic-new', 'attempt_time_text': 'synthetic-old', 'late': False}
DATA = b'%PDF-synthetic-only'


class FakeBrowser:
    sends = 0
    current = CURRENT
    fail = None
    downloaded = DATA
    def __enter__(self): return self
    def __exit__(self, *exc): pass
    def view(self, a):
        value = copy.deepcopy(type(self).current)
        if type(self).sends:
            value['attempt_id'] = '_4_1'; value['attempt_time_text'] = 'synthetic-new'
        return value, '<synthetic>', 'synthetic'
    def download(self, *args): return type(self).downloaded
    def send(self, plan, current, content, target, advance):
        advance('new_attempt_requested')
        if self.fail == 'before_post': raise BBError('synthetic pre-upload failure')
        advance('materials_verified'); advance('submit_requested')
        type(self).sends += 1
        if self.fail == 'lost_response': raise BBError('synthetic response loss')
        return {'http_status': 200}


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = api.Store(Path(self.temp.name))
        for name, value in [('identity', 'synthetic-account'), ('session_binding', 'synthetic-session'), ('locate', A)]:
            p = patch('school_mcp.bb.submissions.' + name, return_value=value); p.start(); self.addCleanup(p.stop)
        FakeBrowser.sends = 0; FakeBrowser.current = copy.deepcopy(CURRENT); FakeBrowser.fail = None; FakeBrowser.downloaded = DATA

    def prepare(self, **kwargs):
        return api.prepare_submission('_1_1', '_2_1', reuse_previous_files=True, resubmit=True,
                                      browser_factory=FakeBrowser, store=self.store, **kwargs)

    def send(self, p):
        return api.submit_assignment(p['preparation_id'], p['content_sha256'], browser_factory=FakeBrowser, store=self.store)

    def test_original_reuse_prepare_send_verify_and_repeat(self):
        p = self.prepare()
        self.assertEqual(p['state'], 'ready'); self.assertEqual(FakeBrowser.sends, 0)
        result = self.send(p)
        self.assertEqual(result['state'], 'verified')
        self.assertTrue(result['result']['files_match'])
        self.send(p); self.assertEqual(FakeBrowser.sends, 1)

    def test_response_loss_requires_read_only_recovery(self):
        p = self.prepare(); FakeBrowser.fail = 'lost_response'
        result = self.send(p); self.assertEqual(result['state'], 'uncertain')
        self.send(p); self.assertEqual(FakeBrowser.sends, 1)
        result = api.submission_status(p['preparation_id'], verify=True, browser_factory=FakeBrowser, store=self.store)
        self.assertEqual(result['state'], 'verified'); self.assertEqual(FakeBrowser.sends, 1)

    def test_changed_attachments_cannot_confirm_success(self):
        p = self.prepare(); FakeBrowser.downloaded = b'other'
        self.assertEqual(self.send(p)['state'], 'uncertain')

    def test_failure_after_new_attempt_is_not_replayed(self):
        p = self.prepare(); FakeBrowser.fail = 'before_post'
        self.assertEqual(self.send(p)['state'], 'uncertain')
        self.send(p); self.assertEqual(FakeBrowser.sends, 0)

    def test_changed_history_stops_before_write(self):
        p = self.prepare(); FakeBrowser.current['attempt_id'] = '_99_1'
        self.assertEqual(self.send(p)['state'], 'stopped_before_write')
        self.assertEqual(FakeBrowser.sends, 0)

    def test_unknown_or_elapsed_deadline_stops(self):
        for due in [None, '2000-01-01T23:59:00+08:00']:
            with self.subTest(due=due):
                FakeBrowser.current['due_at'] = due
                p = self.prepare(); self.assertFalse(p['can_execute'])
                self.assertEqual(self.send(p)['state'], 'stopped_before_write')

    def test_explicit_late_permission(self):
        FakeBrowser.current['due_at'] = '2000-01-01T23:59:00+08:00'; FakeBrowser.current['late'] = True
        p = self.prepare(allow_late=True)
        self.assertTrue(p['can_execute']); self.assertEqual(self.send(p)['state'], 'verified')

    def test_digest_account_and_session_guard(self):
        p = self.prepare()
        with self.assertRaises(BBError): api.submit_assignment(p['preparation_id'], 'wrong', store=self.store)
        with patch('school_mcp.bb.submissions.identity', return_value='another-account'), self.assertRaises(BBError):
            self.store.get(p['preparation_id'])
        with patch('school_mcp.bb.submissions.session_binding', return_value='changed'):
            self.assertEqual(self.send(p)['state'], 'stopped_before_write')

    def test_duplicate_prepare_and_concurrent_claim(self):
        p = self.prepare(); q = self.prepare(); self.assertEqual(p['preparation_id'], q['preparation_id'])
        def claim(_): return api.Store(Path(self.temp.name)).claim(p['preparation_id'], p['content_sha256'])
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(claim, range(4))), 1)

    def test_different_plans_for_same_assignment_share_write_lock(self):
        p = self.prepare(); q = self.prepare(comment='different')
        self.store.claim(p['preparation_id'], p['content_sha256'])
        with self.assertRaises(BBError): self.store.claim(q['preparation_id'], q['content_sha256'])

    def test_local_files_frozen_and_no_unrequested_prior_file(self):
        path = Path(self.temp.name) / 'original.pdf'; path.write_bytes(DATA)
        p = api.prepare_submission('_1_1', '_2_1', [str(path)], resubmit=True, browser_factory=FakeBrowser, store=self.store)
        path.write_bytes(b'changed')
        plan = self.store.get(p['preparation_id'])['plan']
        self.assertEqual(base64.b64decode(plan['files'][0]['data']), DATA)
        with self.assertRaises(BBError): api.prepare_submission('_1_1', '_2_1', [str(path)], reuse_previous_files=True, resubmit=True, browser_factory=FakeBrowser, store=self.store)

    def test_first_submission_only_with_pristine_form(self):
        FakeBrowser.current.update(kind='new_form', attempt_id=None, files=[], new_url=None)
        path = Path(self.temp.name) / 'original.pdf'; path.write_bytes(DATA)
        p = api.prepare_submission('_1_1', '_2_1', [str(path)], browser_factory=FakeBrowser, store=self.store)
        self.assertFalse(p['resubmit'])
        with self.assertRaises(BBError): self.prepare()

    def test_expired_preparation_stops(self):
        p = self.prepare()
        with patch('school_mcp.bb.submissions.now', return_value=api.now() + timedelta(days=2)):
            self.assertEqual(self.send(p)['state'], 'stopped_before_write')

    def test_dpapi_payload_not_plaintext_on_disk(self):
        self.prepare(comment='synthetic-private-comment')
        self.assertNotIn(b'synthetic-private-comment', self.store.path.read_bytes())
        self.assertNotIn(DATA, self.store.path.read_bytes())

    def test_filename_and_deadline_contracts(self):
        self.assertEqual(deadline('2026年6月30日 星期二 下午11:59'), '2026-06-30T23:59:00+08:00')
        self.assertEqual(deadline('2026年6月30日 上午12:00'), '2026-06-30T00:00:00+08:00')
        self.assertIsNone(deadline('下周')); self.assertIsNone(deadline('2026年99月3日 10:00'))
        for name in ['../private', 'dir\\file', '', 'x\n.pdf']:
            with self.assertRaises(BBError): file_record(name, DATA)

    def test_static_allowlist_does_not_allow_business_or_other_hosts(self):
        self.assertTrue(static_request('https://www.bb.ustc.edu.cn/javascript/a.js', 'GET'))
        for url, method in [('https://evil.example/javascript/a.js', 'GET'),
                            ('https://www.bb.ustc.edu.cn/webapps/assignment/uploadAssignment?action=newAttempt', 'GET'),
                            ('https://www.bb.ustc.edu.cn/javascript/a.js', 'POST')]:
            self.assertFalse(static_request(url, method))

    def test_multipart_checks_target_files_and_dispatch_without_cdp_file_bytes(self):
        p = self.prepare(); plan = self.store.get(p['preparation_id'])['plan']
        def body(dispatch='submit', course='_1_1', filename='original.pdf'):
            fields = {'course_id':course,'content_id':'_2_1','dispatch':dispatch,'attempt_id':'','remove_file_id':'','student_commentstext':''}
            chunks=[f'--b\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k,v in fields.items()]
            return (''.join(chunks)+f'--b\r\nContent-Disposition: form-data; name="newFile_LocalFile0"; filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n\r\n--b--\r\n').encode()
        check_multipart(body(), 'multipart/form-data; boundary=b', plan)
        for kwargs in [{'dispatch':'save'}, {'course':'_9_1'}, {'filename':'wrong.pdf'}]:
            with self.assertRaises(BBError): check_multipart(body(**kwargs), 'multipart/form-data; boundary=b', plan)
