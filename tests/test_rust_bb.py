"""Real-pipe BB parser parity and synthetic one-shot submission execution."""
import os
from pathlib import Path
from datetime import timedelta
import unittest
from unittest.mock import Mock, patch

import httpx

from school_mcp.bb import native
from school_mcp.bb import parsing, assignments, assignment_review, submission_contracts
from school_mcp.bb.client import BBClient
from school_mcp.bb.session import BBError
from school_mcp.desktop import native_core
from school_mcp.desktop.model import ModelError
from test_bb import PORTAL, COOKIES
from test_bb_assignments import HTML as ASSIGNMENTS, PATH as CONTENT_PATH
from test_bb_assignment_review import HTML as REVIEW, PATH as REVIEW_PATH
import test_bb_submissions as legacy
from test_bb_submissions import A
from smoke_bb_submission import FORM, history
from network_test_support import isolate_pacing as setUpModule

CORE = Path(os.environ.get('SCHOOL_TEST_CORE', Path(__file__).resolve().parents[1] / 'native/core/target/debug/school-core.exe'))


class NativeFixture:
    def setUp(self):
        native_core.shutdown()
        env = patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': str(CORE)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(native_core.shutdown)
        super().setUp()


@unittest.skipUnless(CORE.is_file(), 'build native/core first')
class RustBBTests(NativeFixture, unittest.TestCase):
    def compare(self, method, *args, **kwargs):
        with patch.object(native, 'enabled', return_value=False):
            expected = method(*args, **kwargs)
        actual = method(*args, **kwargs)
        self.assertEqual(actual, expected)
        return actual

    def test_courses_page_text_unicode_menu_and_secret_filter_parity(self):
        self.compare(parsing.course_links, PORTAL)
        html = ('<title>课程</title><div id="courseMenuPalette_contents">'
                '<a href="/webapps/blackboard/content/listContent.jsp?course_id=_1_1&amp;content_id=_2_1&amp;mode=view&amp;mode=reset&amp;csrf_token=synthetic">讲义</a></div>'
                '<main>通知😀<script>private-script</script><input value="private-field">'
                '<textarea>private-draft</textarea><a href="/bbcswebdav/example.pdf">附件</a></main>')
        result = self.compare(parsing.page_data, html, 'https://www.bb.ustc.edu.cn/webapps/blackboard/execute/modulepage/view', max_chars=3)
        self.assertEqual(result['text'], '通知😀')
        self.assertNotIn('private', str(result))
        self.assertNotIn('synthetic', str(result))
        self.assertTrue(result['links'][0]['course_menu'])

    def test_client_uses_rust_http_and_rust_parser(self):
        response = httpx.Response(200, text=PORTAL, request=httpx.Request('GET', 'https://www.bb.ustc.edu.cn/webapps/portal/execute/tabs/tabAction'))
        with patch('school_mcp.authenticated_native.request', return_value=response) as request, patch('httpx.Client', side_effect=AssertionError('Python network forbidden')):
            client = BBClient(COOKIES)
            result = client.courses(keyword='量子', term='2026FA')
            self.assertEqual(result['count'], 1)
            self.assertEqual(request.call_args.args[0], 'bb')
            self.assertEqual(request.call_args.args[2], COOKIES)
            self.assertTrue(client.check()['connected'])

    def test_invalid_urls_and_identifiers_stop_before_http(self):
        with patch('school_mcp.authenticated_native.request') as request:
            for path in ['https://other.invalid/x', '//other.invalid/x', '/x\r\nInjected: value',
                         '/webapps/blackboard/execute/announcement?method=deleteAnnouncement']:
                with self.subTest(path=path), self.assertRaises(BBError):
                    BBClient(COOKIES).read_page(path)
            with self.assertRaises(BBError): BBClient(COOKIES).course_page('_1_1&action=delete')
            request.assert_not_called()

    def test_catalog_drives_only_bounded_same_course_content_io(self):
        self.compare(assignments.parse_assignments, ASSIGNMENTS, '_1_1')
        nested = '/webapps/blackboard/content/listContent.jsp?course_id=_1_1&content_id=_4_1'
        client = Mock()
        client.course_page.return_value = {'links': [{'course_menu': True, 'internal': True, 'path': CONTENT_PATH}]}
        client.get.return_value = Mock(text=ASSIGNMENTS + f'<a href="{CONTENT_PATH}">self</a><a href="{nested}">folder</a>')
        result = self.compare(assignments.catalog, '_1_1', max_pages=1, client=client)
        self.assertTrue(result['truncated'])
        self.assertFalse(result['attempts_opened'])
        self.assertEqual([c.args[0] for c in client.get.call_args_list], [CONTENT_PATH, CONTENT_PATH])
        self.assertFalse(assignments.parse_assignments(ASSIGNMENTS.replace('course_id=_1_1', 'course_id=_9_1'), '_1_1'))

    def test_review_history_is_not_new_receipt(self):
        for names in [False, True]:
            result = self.compare(assignment_review.parse_review, REVIEW, include_attachment_names=names)
            self.assertFalse(result['submitted_by_this_call'])
        self.compare(assignment_review.parse_review, '<h1>different template</h1>')
        self.compare(assignment_review.review_url, {'entry_path': REVIEW_PATH}, '_1_1', '_2_1')
        for bad in [REVIEW_PATH+'&action=newAttempt', REVIEW_PATH+'&course_id=_9_1', REVIEW_PATH.replace('group_id=', 'group_id=_3_1')]:
            with self.assertRaises(BBError): assignment_review.review_url({'entry_path':bad}, '_1_1', '_2_1')

    def test_form_snapshot_history_download_and_deadline_parity(self):
        self.compare(submission_contracts.form_contract, FORM, A)
        self.compare(submission_contracts.snapshot, FORM, A)
        self.compare(submission_contracts.snapshot, history('_3_1'), A)
        for due in ['2026年6月30日 星期二 下午11:59', '2026年6月30日 上午12:00', '2026年99月3日 10:00', '下周']:
            self.compare(submission_contracts.deadline, due)
        for bad in [FORM.replace('value="_2_1"', 'value="_99_1"'), FORM.replace('name="attempt_id" value=""', 'name="attempt_id" value="_3_1"'),
                    FORM.replace('<textarea id="studentSubmission.text" name="studentSubmission.text"></textarea>', '<textarea name="studentSubmission.text">existing draft</textarea>')]:
            with self.assertRaises(BBError): submission_contracts.form_contract(bad, A)
        good = '/webapps/assignment/download?course_id=_1_1&attempt_id=_3_1&file_id=_5_1'
        self.compare(submission_contracts.validate_download, good, A, '_3_1')
        for bad in [good+'&action=delete', good.replace('attempt_id=_3_1', 'attempt_id=_99_1'), good+'#fragment']:
            with self.assertRaises(BBError): submission_contracts.validate_download(bad, A, '_3_1')

    def test_large_snapshot_bytes_never_enter_rust_business_pipe(self):
        plan = {'files': [{'filename':'a.pdf','size_bytes':1,'sha256':'f'*64,'data':'private-file-canary'}]}
        with patch.object(native_core, 'call', return_value={}) as call:
            native.call('status', job={'plan':plan})
            self.assertNotIn('private-file-canary', str(call.call_args))
            self.assertEqual(plan['files'][0]['data'], 'private-file-canary')

    def test_rust_contract_rejects_invalid_state_and_excessive_html(self):
        with self.assertRaises(ModelError):
            native_core.call('bb_adapter', {'action':'catalog_step','course_id':'_1_1','max_pages':1,'limit':1,'state':{'pending':'/wrong','queue':[],'visited':[],'assignments':[]},'path':'/changed','html':''})
        with self.assertRaises(BBError):
            parsing.page_data('x'*(5*1024*1024+1), 'https://www.bb.ustc.edu.cn/', 10)


@unittest.skipUnless(CORE.is_file(), 'build native/core first')
class RustBBSubmissionTests(NativeFixture, legacy.SubmissionTests):
    """Reuse actual transaction/replay contracts with Rust decisions enabled."""

    def test_rust_failure_cannot_fall_back_to_python_write(self):
        preparation = self.prepare()
        original = native_core.call
        def fail(operation, value):
            if operation == 'bb_adapter' and value['action'] == 'preflight':
                raise ModelError('runtime_failure', 'synthetic Rust failure')
            return original(operation, value)
        with patch.object(native_core, 'call', side_effect=fail):
            result = self.send(preparation)
        self.assertEqual(result['state'], 'stopped_before_write')
        from test_bb_submissions import FakeBrowser
        self.assertEqual(FakeBrowser.sends, 0)

    def test_future_preparation_stops_after_clock_rollback(self):
        preparation = self.prepare()
        with patch('school_mcp.bb.submissions.now', return_value=legacy.api.now() - timedelta(hours=1)):
            result = self.send(preparation)
        self.assertEqual(result['state'], 'stopped_before_write')
        self.assertIn('晚于当前时间', result['result']['message'])
        self.assertEqual(legacy.FakeBrowser.sends, 0)
