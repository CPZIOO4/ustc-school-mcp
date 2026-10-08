import asyncio
import unittest
from unittest.mock import patch

from school_mcp.bb.assignment_review import parse_review, review_request_allowed, review_url
from school_mcp.bb.assignments import parse_assignments
from school_mcp.bb.session import BBError

PATH = '/webapps/assignment/uploadAssignment?course_id=_1_1&content_id=_2_1&mode=view&group_id='
HTML = '''<h1 id="pageTitleHeader">复查提交历史记录: 合成作业</h1>
<div id="assignmentInfo"><h3>截止日期</h3><p>2026年1月2日 下午11:59</p></div>
<h3 id="currentAttempt_label"><span class="mainLabel">尝试(逾期)</span>
<span class="dateStamp">26-1-3 上午10:00</span></h3>
<ul id="currentAttempt_submissionList"><li><a class="attachment">synthetic.pdf</a></li></ul>
<p id="bottom_submitButtonRow"><input type="submit" value="开始新的" onclick="secret-action"/></p>
<input name="csrf" value="synthetic-secret"/><script>privateData="hidden-value"</script>'''


class AssignmentReviewTests(unittest.TestCase):
    def test_mcp_browser_work_runs_outside_async_event_loop(self):
        from school_mcp.bb.server import school_bb_inspect_assignment
        def inspect(*args, **kwargs):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return {'verified_thread': True}
        with patch('school_mcp.bb.server.inspect_assignment', side_effect=inspect):
            result = asyncio.run(school_bb_inspect_assignment('_1_1', '_2_1'))
        self.assertTrue(result['verified_thread'])

    def test_finance_browser_uses_same_thread_boundary(self):
        from school_mcp.finance.server import school_finance_inspect_smart
        def inspect():
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return {'verified_thread': True}
        with patch('school_mcp.finance.server.FinanceClient') as client:
            client.return_value.inspect_smart.side_effect = inspect
            result = asyncio.run(school_finance_inspect_smart())
        self.assertTrue(result['verified_thread'])

    def test_exact_observed_personal_view_only(self):
        self.assertTrue(review_url({'entry_path': PATH}, '_1_1', '_2_1').startswith('https://www.bb.ustc.edu.cn/'))
        for path in [PATH + '&action=newAttempt', PATH.replace('mode=view', 'mode=reset'),
                     PATH + '&course_id=_9_1', PATH.replace('group_id=', 'group_id=_3_1'),
                     PATH.replace('_2_1', '_99_1'), PATH + '#fragment',
                     'https://evil.example' + PATH, PATH.replace('uploadAssignment', 'submitAssignment')]:
            with self.subTest(path=path), self.assertRaises(BBError):
                review_url({'entry_path': path}, '_1_1', '_2_1')

    def test_guard_blocks_writes_resources_iframes_and_new_attempt(self):
        self.assertTrue(review_request_allowed(PATH, 'GET', PATH, navigation=True, main_frame=True))
        for url, method, nav, main in [(PATH, 'POST', True, True), (PATH + '&action=newAttempt', 'GET', True, True),
                                       (PATH, 'GET', False, True), (PATH, 'GET', True, False),
                                       ('https://evil.example', 'GET', True, True)]:
            self.assertFalse(review_request_allowed(url, method, PATH, navigation=nav, main_frame=main))

    def test_history_is_evidence_not_current_submission_receipt(self):
        result = parse_review(HTML)
        self.assertEqual(result['submission_state'], 'historical_attempt_present')
        self.assertEqual(result['attempt_scope'], 'currently_displayed_attempt')
        self.assertTrue(result['late_label_present'])
        self.assertTrue(result['new_attempt_control_present'])
        self.assertFalse(result['submitted_by_this_call'])
        self.assertFalse(result['new_attempt_requested'])
        self.assertFalse(result['catalog_complete'])
        self.assertEqual(result['attachment_count'], 1)
        self.assertNotIn('attachment_names', result)
        self.assertNotIn('synthetic-secret', str(result))
        self.assertNotIn('hidden-value', str(result))

    def test_explicit_attachment_names_and_due_text(self):
        result = parse_review(HTML, include_attachment_names=True)
        self.assertEqual(result['attachment_names'], ['synthetic.pdf'])
        self.assertEqual(result['due_text'], ['2026年1月2日 下午11:59'])
        self.assertEqual(result['attempt_time_text'], '26-1-3 上午10:00')

    def test_unknown_page_is_not_unsubmitted_or_safe_upload(self):
        result = parse_review('<h1 id="pageTitleHeader">上传作业</h1><input type="file"><p>private-text</p>')
        self.assertEqual(result['submission_state'], 'unknown')
        self.assertEqual(result['page_kind'], 'unverified_page')
        self.assertFalse(result['upload_supported'])
        self.assertNotIn('private-text', str(result))

    def test_draft_continue_and_missing_fields_remain_uncertain(self):
        result = parse_review('''<h1 id="pageTitleHeader">Review Submission History: synthetic</h1>
        <p id="bottom_submitButtonRow"><input value="Continue"></p>''')
        self.assertTrue(result['continue_control_present'])
        self.assertFalse(result['due_text_available'])
        self.assertIsNone(result['attempt_time_text'])
        self.assertEqual(result['submission_state'], 'unknown')

    def test_submit_time_wording_retained_as_raw_deadline_evidence(self):
        html = f'<li><a href="{PATH}">作业</a><p>提交时间为5.31 23：59。</p></li>'
        result = parse_assignments(html, '_1_1')[0]
        self.assertTrue(result['due_text_available'])
        self.assertEqual(result['due_text'], ['提交时间为5.31 23：59。'])
