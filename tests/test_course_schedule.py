from copy import deepcopy
import unittest
from unittest.mock import patch

from school_mcp.jw.course_schedule import course_schedule, select_courses, bb_term, select_bb, announcement_evidence

COURSE = {'lesson_id': 11, 'course_name': '模型原理', 'course_code': 'TEST101', 'class_code': 'TEST101.01'}
SEMESTER = {'id': 9, 'name': '2026年秋季学期'}
ACTIVITY = {**COURSE, 'weekday': 6, 'date': '2026-10-10', 'start': '09:00', 'end': '11:00', 'room': '示例教室'}
BB = {'course_id': '_7_1', 'title': 'TEST101.01.2026FA: 模型原理', 'path': '/example'}


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.rows = [deepcopy(COURSE)]
        self.activities = [deepcopy(ACTIVITY), {**ACTIVITY, 'lesson_id': 99, 'course_name': '模型概述'}]
        self.bb = [deepcopy(BB)]
        self.page = {'text': '课程介绍', 'url': 'https://www.bb.ustc.edu.cn/example', 'text_truncated': False}
        self.failures = {}
        self.read = self.enterContext(patch('school_mcp.jw.course_schedule.ReadSession.read', side_effect=self.dispatch))

    def dispatch(self, service, operation, params):
        if (service, operation) in self.failures:
            return self.failures[(service, operation)]
        values = {('jw', 'courses'): {'semester': SEMESTER, 'courses': self.rows},
                  ('jw', 'timetable'): {'activities': self.activities},
                  ('bb', 'courses'): {'courses': self.bb}, ('bb', 'announcements'): self.page}
        return {'state': 'completed', 'completed': True, 'data': values[(service, operation)]}

    def run_query(self, **kwargs):
        return course_schedule('模型原理', week=6, weekday=6, **kwargs)

    def test_specific_lesson_only_and_official_semester(self):
        result = self.run_query()
        self.assertEqual(result['state'], 'confirmed')
        self.assertEqual([r['lesson_id'] for r in result['timetable']], [11])
        self.read.assert_any_call('jw', 'timetable', {'semester_id': 9, 'week': 6, 'weekday': 6})
        self.read.assert_any_call('bb', 'courses', {'term': '2026FA'})

    def test_ambiguous_class_stops_before_timetable(self):
        self.rows.append({**COURSE, 'lesson_id': 12, 'class_code': 'TEST101.02'})
        result = self.run_query()
        self.assertEqual(result['state'], 'ambiguous')
        self.assertEqual(self.read.call_count, 1)
        self.assertEqual(self.run_query(lesson_id=11)['course']['lesson_id'], 11)

    def test_unmatched_name_does_not_substitute_similar_course(self):
        self.rows = [{**COURSE, 'course_name': '模型概述'}]
        self.assertEqual(self.run_query()['state'], 'insufficient_evidence')
        self.assertEqual(self.read.call_count, 1)

    def test_exact_name_precedes_partial_name(self):
        self.rows.append({**COURSE, 'lesson_id': 12, 'course_name': '模型原理实验'})
        self.assertEqual(self.run_query()['course']['lesson_id'], 11)

    def test_lecture_is_not_lab(self):
        result = self.run_query(focus='experiment')
        self.assertEqual(result['state'], 'insufficient_evidence')
        self.assertEqual(result['timetable'], [])
        self.activities[0]['remark'] = '上机实验'
        self.assertEqual(self.run_query(focus='experiment')['state'], 'confirmed')

    def test_missing_or_wrong_bb_section_is_unconfirmed(self):
        for title in ['TEST101.01(2025FA): 模型原理', 'TEST101.010(2026FA): 模型原理', '模型原理(2026FA)']:
            self.bb[0]['title'] = title
            result = self.run_query()
            self.assertEqual(result['state'], 'insufficient_evidence')
            self.assertEqual(result['sources']['bb']['state'], 'course_mapping_required')

    def test_bb_failure_preserves_timetable_with_explicit_incompleteness(self):
        self.failures[('bb', 'courses')] = {'completed': False, 'state': 'cooldown', 'retry_after_seconds': 60}
        result = self.run_query()
        self.assertTrue(result['partial'])
        self.assertEqual(result['state'], 'insufficient_evidence')
        self.assertEqual(len(result['timetable']), 1)
        self.assertEqual(result['sources']['bb']['retry_after_seconds'], 60)

    def test_no_timetable_is_not_no_lab(self):
        self.activities = [self.activities[1]]
        self.assertEqual(self.run_query()['state'], 'insufficient_evidence')

    def test_announcement_dates_are_not_promoted_to_class_times(self):
        self.page['text'] = '发布时间：2026年10月9日 09:00\n实验安排\n下周开始，上交作业截止时间11:00\n地点另行通知'
        result = self.run_query()
        self.assertEqual(result['state'], 'ambiguous')
        self.assertTrue(result['announcements'][0]['requires_review'])
        self.assertEqual(result['timetable'][0]['start'], '09:00')

    def test_cancellation_or_truncation_cannot_confirm(self):
        self.page['text'] = '取消'
        self.assertEqual(self.run_query()['state'], 'ambiguous')
        self.page.update(text='课程介绍', text_truncated=True)
        self.assertEqual(self.run_query()['state'], 'insufficient_evidence')

    def test_custom_dates_and_missing_dates_need_review(self):
        self.activities[0]['self_defined_dates'] = True
        self.assertEqual(self.run_query()['state'], 'ambiguous')
        self.activities[0].pop('self_defined_dates')
        self.activities[0].pop('date')
        self.assertEqual(self.run_query()['state'], 'ambiguous')

    def test_bb_opt_out_is_explicit_scope(self):
        result = self.run_query(include_bb=False)
        self.assertEqual(result['state'], 'confirmed')
        self.assertFalse(result['scope']['include_bb'])
        self.assertNotIn('bb', result['sources'])

    def test_notice_output_is_bounded_with_coverage(self):
        self.page['text'] = ('实验时间\n' + '测试' * 100 + '\n') * 100
        evidence = announcement_evidence(self.page)
        self.assertTrue(evidence['truncated'])
        self.assertLessEqual(sum(len(r['excerpt']) for r in evidence['items']), 5000)

    def test_input_validation_precedes_network(self):
        for query, kwargs in [(' ', {}), ('模型', {'weekday': 8}), ('模型', {'lesson_id': True})]:
            with self.assertRaises(ValueError):
                course_schedule(query, **kwargs)
        self.read.assert_not_called()

    def test_unknown_semester_mapping_is_not_guessed(self):
        self.assertIsNone(bb_term({'name': '2025-2026学年第二学期'}))
        self.assertEqual(bb_term(SEMESTER), '2026FA')


if __name__ == '__main__':
    unittest.main()
