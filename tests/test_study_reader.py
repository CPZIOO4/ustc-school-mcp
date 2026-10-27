import unittest
from unittest.mock import patch, MagicMock
from school_mcp.desktop.study_reader import read
from school_mcp.bb.session import BBError

class StudyReaderTests(unittest.TestCase):
    def test_history_timeout_is_partial_read_with_fixed_guidance(self):
        stop=MagicMock();stop.is_set.return_value=False
        notify=MagicMock()
        with patch('school_mcp.desktop.study_reader.load_credentials',return_value={'username':'synthetic'}),patch('school_mcp.desktop.study_reader.BBClient'),patch('school_mcp.desktop.study_reader.record',return_value={'error_id':'ERR-0123456789ABCDEF'}) as record,patch('school_mcp.desktop.study_reader.inspect_assignment',side_effect=BBError('private cookie',code='timeout')) as inspect:
            v=read({'scope':'assignment','course_id':'_1_1','content_id':'_2_1'},stop,notify)
        self.assertEqual(v['verification_error']['code'],'timeout')
        self.assertNotIn('verification',v)
        self.assertNotIn('private cookie',str(v))
        self.assertIn('核实提交记录',v['verification_error']['message'])
        inspect.assert_called_once()
        self.assertEqual(record.call_args.kwargs['outcome'],'read_failed')

    def test_authentication_failure_still_reaches_recovery(self):
        stop=MagicMock();stop.is_set.return_value=False
        with patch('school_mcp.desktop.study_reader.load_credentials',return_value={'username':'synthetic'}),patch('school_mcp.desktop.study_reader.BBClient'),patch('school_mcp.desktop.study_reader.inspect_assignment',side_effect=BBError('需要认证',code='authentication_required')):
            with self.assertRaises(BBError):read({'scope':'assignment','course_id':'_1_1','content_id':'_2_1'},stop,lambda *_:None)

    def test_partial_failure_keeps_other_sections_and_has_no_submit(self):
        client=MagicMock()
        client.course_page.return_value={'text':'课程主页'}
        client.announcements.side_effect=RuntimeError('private URL must not leak')
        stop=MagicMock();stop.is_set.return_value=False
        with patch('school_mcp.desktop.study_reader.record'), patch('school_mcp.desktop.study_reader.load_credentials',return_value={'username':'synthetic'}),patch('school_mcp.desktop.study_reader.BBClient',return_value=client),patch('school_mcp.desktop.study_reader.catalog',return_value={'assignments':[],'truncated':False}):
            v=read({'scope':'course','course_id':'_1_1'},stop,lambda *_:None)
        self.assertEqual(v['catalog']['assignments'],[])
        self.assertIn('announcements',v['errors'])
        self.assertNotIn('private URL',str(v))

    def test_page_read_is_course_bound_and_read_only(self):
        stop=MagicMock();stop.is_set.return_value=False
        with patch('school_mcp.desktop.study_reader.record'), patch('school_mcp.desktop.study_reader.load_credentials',return_value={'username':'synthetic'}),patch('school_mcp.desktop.study_reader.BBClient') as client:
            for path in ['/webapps/assignment/uploadAssignment?course_id=_1_1','/webapps/blackboard/content/listContent.jsp?course_id=_2_1']:
                with self.assertRaises(ValueError):read({'scope':'page','course_id':'_1_1','path':path},stop,lambda *_:None)
            client.return_value.read_page.assert_not_called()

    def test_stopped_read_does_not_access_school(self):
        stop=MagicMock();stop.is_set.return_value=True
        with patch('school_mcp.desktop.study_reader.record'), patch('school_mcp.desktop.study_reader.load_credentials',return_value={'username':'synthetic'}),patch('school_mcp.desktop.study_reader.BBClient') as client:
            with self.assertRaises(ValueError):read({'scope':'courses'},stop,lambda *_:None)
            client.return_value.courses.assert_not_called()

if __name__=='__main__':unittest.main()
