import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from school_mcp.bb.assignments import catalog, parse_assignments, prepare_files
from school_mcp.bb.session import BBError

PATH = '/webapps/blackboard/content/listContent.jsp?course_id=_1_1&content_id=_2_1'
HTML = '''<ul><li><h3><a href="/webapps/assignment/uploadAssignment?course_id=_1_1&amp;content_id=_3_1&amp;nonce=private">作业一</a></h3>
<div>提交PDF。截止时间：2026年10月20日。</div></li></ul>'''


class AssignmentTests(unittest.TestCase):
    def client(self, html=HTML):
        client = Mock()
        client.course_page.return_value = {'links': [{'course_menu': True, 'internal': True, 'path': PATH}]}
        client.get.return_value = Mock(text=html)
        return client

    def test_parse_preserves_evidence_without_guessing_submission(self):
        item = parse_assignments(HTML, '_1_1')[0]
        self.assertEqual(item['content_id'], '_3_1')
        self.assertTrue(item['due_text_available'])
        self.assertEqual(item['submission_state'], 'not_checked')
        self.assertNotIn('nonce', item['entry_path'])

    def test_external_and_cross_course_entries_not_followed(self):
        html = HTML.replace('course_id=_1_1', 'course_id=_9_1')
        self.assertFalse(parse_assignments(html, '_1_1'))
        self.assertFalse(parse_assignments(HTML.replace('/webapps/assignment', 'https://evil.example/webapps/assignment'), '_1_1'))

    def test_catalog_only_gets_content_not_attempt(self):
        client = self.client()
        result = catalog('_1_1', client=client)
        self.assertEqual(result['status'], 'entries_found')
        self.assertFalse(result['attempts_opened'])
        client.get.assert_called_once_with(PATH)

    def test_nested_cycle_and_page_limit_are_explicit(self):
        other = '/webapps/blackboard/content/listContent.jsp?course_id=_1_1&content_id=_4_1'
        client = self.client(HTML + f'<a href="{PATH}">self</a><a href="{other}">folder</a>')
        result = catalog('_1_1', max_pages=1, client=client)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['pages_checked'], 1)

    def test_empty_catalog_is_scoped_not_no_homework_claim(self):
        client = self.client('<html>主页</html>')
        result = catalog('_1_1', client=client)
        self.assertEqual(result['status'], 'not_found_in_scanned_pages')
        self.assertEqual(result['scope'], 'reachable_course_content_pages')

    def test_files_are_frozen_locally_without_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'assignment.pdf'
            path.write_bytes(b'synthetic-document')
            with patch('school_mcp.bb.assignments.load_credentials', return_value={'username': 'synthetic'}), \
                 patch('school_mcp.bb.assignments._save') as save:
                result = prepare_files('_1_1', '_3_1', [str(path)], client=self.client())
                path.write_bytes(b'changed later')
                self.assertEqual(base64.b64decode(save.call_args.args[1]['files'][0]['data']), b'synthetic-document')
                self.assertEqual(result['status'], 'prepared_locally')
                self.assertFalse(result['uploaded'])
                self.assertFalse(result['submitted'])
                self.assertFalse(result['upload_supported'])

    def test_unknown_assignment_not_prepared(self):
        with self.assertRaises(BBError): prepare_files('_1_1', '_999_1', ['/synthetic'], client=self.client())

    def test_duplicate_filenames_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'assignment.txt'; path.write_text('test')
            with self.assertRaises(BBError): prepare_files('_1_1', '_3_1', [str(path), str(path)], client=self.client())
