"""Real Rust pipe contracts for personal academic reads and timetable planning.

All fixtures are synthetic. HTTP is isolated; shared authenticated HTTP has its
own Rust loopback tests. These tests cannot read local accounts or enrol courses.
"""
import copy
import json
import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

import httpx
from network_test_support import isolate_pacing as setUpModule

from school_mcp.desktop import native_core
from school_mcp.desktop.model import ModelError
from school_mcp.jw.client import JWClient
from school_mcp.jw.academic import AcademicClient, schedule_slots
from school_mcp.jw.planner import Candidate, Constraints, Slot, generate
from school_mcp.jw.session import JWError
from test_jw import Fixture, CONTEXT, COURSES, TIMETABLE, GRADES
import test_business_workflows as workflow_fixtures
from test_business_workflows import candidate

CORE = Path(os.environ.get('SCHOOL_TEST_CORE', Path(__file__).resolve().parents[1] / 'native/core/target/debug/school-core.exe'))


@unittest.skipUnless(CORE.is_file(), 'build native/core first')
class RustJWTests(unittest.TestCase):
    def setUp(self):
        native_core.shutdown()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': str(CORE), 'SCHOOL_MCP_LOCAL_DIR': self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(native_core.shutdown)

    def call(self, action, **arguments):
        return native_core.call('jw', {'action': action, **arguments})

    def legacy(self, callback):
        with patch.object(native_core, 'enabled', return_value=False):
            return callback()

    def response_bridge(self, legacy_get):
        def request(service, path, cookies, user_agent, error_type, params=None, method='GET'):
            self.assertEqual(service, 'jw')
            # Fixture transport performs no network. Actual response interpretation
            # happens after this bridge through the Rust pipe.
            return self.legacy(lambda: legacy_get(path, params, method=method))
        return request

    def test_context_courses_timetable_and_masked_grade_parity(self):
        fixture = Fixture()
        bridge = self.response_bridge(lambda p, q, method: fixture.client.get(p, q))
        current = JWClient(cookies=[], user_agent='SyntheticBrowser/1')
        cases = [
            ('check', {}), ('home', {}), ('context', {}), ('semesters', {}),
            ('courses', {}), ('courses', {'semester_id': 4, 'keyword': '量子'}),
            ('timetable', {'week': 2, 'weekday': 7}), ('timetable', {'week': 1}),
            ('grades', {}), ('grades', {'keyword': '量子'}), ('grades', {'summary_only': True}),
            ('grades', {'semester_id': 5}),
        ]
        for method, arguments in cases:
            expected = self.legacy(lambda: getattr(fixture.client, method)(**arguments))
            with self.subTest(method=method, arguments=arguments), patch('school_mcp.authenticated_native.request', side_effect=bridge):
                self.assertEqual(getattr(current, method)(**arguments), expected)
        with patch('school_mcp.authenticated_native.request', side_effect=bridge):
            result = current.grades()
        self.assertNotIn('synthetic-hidden-score', json.dumps(result))
        self.assertNotIn('synthetic-report-secret', json.dumps(result))
        self.assertIsNone(result['grades'][1]['score'])

    def test_native_paths_do_not_use_python_parsers(self):
        fixture = Fixture()
        bridge = self.response_bridge(lambda p, q, method: fixture.client.get(p, q))
        current = JWClient(cookies=[], user_agent='SyntheticBrowser/1')
        # Materialise wire responses before disabling the legacy parser, because
        # the fixture redirect helper itself uses that parser for auth detection.
        response_cache = {}
        for p, q in [('/for-std/course-table', None), ('/for-std/course-table/get-data', {'bizTypeId': 2, 'semesterId': 5, 'dataId': 73})]:
            response_cache[p] = bridge('jw', p, [], '', JWError, q)
        def read(service, path, *args, **kwargs):
            return response_cache[path]
        with patch('school_mcp.authenticated_native.request', side_effect=read), patch('school_mcp.jw.client.BeautifulSoup', side_effect=AssertionError('Python parser forbidden')), patch('school_mcp.jw.client.lesson_row', side_effect=AssertionError('Python mapper forbidden')):
            self.assertEqual(current.courses()['courses'][0]['course_name'], '量子物理')

    def test_offer_pagination_schedule_and_window_parity(self):
        fixture = workflow_fixtures.AcademicTests().client()
        bridge = self.response_bridge(fixture.request)
        current = AcademicClient(JWClient(cookies=[], user_agent='SyntheticBrowser/1'))
        for method, arguments in [('offerings', (1, 'synthetic', 2, 3)), ('window', ())]:
            expected = self.legacy(lambda: getattr(fixture, method)(*arguments))
            with patch('school_mcp.authenticated_native.request', side_effect=bridge):
                self.assertEqual(getattr(current, method)(*arguments), expected)
        wrong_page = workflow_fixtures.AcademicTests().client(page_mismatch=True)
        with patch('school_mcp.authenticated_native.request', side_effect=self.response_bridge(wrong_page.request)), self.assertRaisesRegex(JWError, '页码'):
            current.offerings(1, 'synthetic', 2, 3)
        unknown_schedule = workflow_fixtures.AcademicTests().client(bad_schedule=True)
        with patch('school_mcp.authenticated_native.request', side_effect=self.response_bridge(unknown_schedule.request)):
            result = current.offerings(1, 'synthetic', 2, 3)
        self.assertFalse(result['offerings'][0]['schedule_known'])

    def test_planning_context_and_changes_remain_preflight_only(self):
        fixture = Fixture()
        fixture_api = AcademicClient(fixture.client)
        context, data = self.legacy(lambda: fixture.client.course_data())
        expected = self.legacy(lambda: fixture_api.planning_context(5))
        current = AcademicClient(JWClient(cookies=[], user_agent='SyntheticBrowser/1'))
        with patch.object(current.jw, 'course_data', return_value=(context, data)):
            self.assertEqual(current.planning_context(5), expected)
        data = copy.deepcopy(data)
        data['lessons'][0].pop('credits')
        with patch.object(current.jw, 'course_data', return_value=(context, data)):
            missing = current.planning_context(5)
        self.assertEqual(missing['state'], 'needs_input')
        self.assertFalse(missing['input_guidance']['mutation_authorized'])
        courses = self.legacy(lambda: fixture.client.courses())
        for action, lesson_id in [('drop', 11), ('select', 11), ('drop', 77), ('select', 77)]:
            for turns in [[], [{'id': 1, 'name': 'Synthetic', 'allowEnter': True, 'unrelated_private': 'omit'}]]:
                window = self.call('window', data=turns)
                with patch.object(current.jw, 'courses', return_value=courses), patch.object(current, 'window', return_value=window):
                    result = current.prepare_change(action, 5, lesson_id)
                self.assertEqual(result['state'], 'blocked')
                self.assertFalse(result['can_execute'])
                self.assertFalse(result['mutation_performed'])
                self.assertIn('live_selection_contract_not_verified', result['blockers'])
                self.assertNotIn('unrelated_private', json.dumps(result))

    def test_schedule_parser_parity_disjoint_weeks_periods_and_unknown(self):
        for value in ['1~3,6~8周 A101 :2(1,3) 合成教师', '1-8周 教室：1(20)', '1,4周 A :7(2,2,4)\n5~6周 B :2(1)', '', '待定', '1~8单周 A :2(1,2)', '1~61周 A :2(1)', '1~2周 A :2(0)', '3~1周 A :2(1)']:
            expected = self.legacy(lambda: schedule_slots(value, 'east'))
            self.assertEqual(self.call('schedule_slots', text=value, campus='east'), expected)

    def test_planner_parity_random_constraints_and_conflict_ranking(self):
        randomizer = random.Random(608)
        for index in range(45):
            items = [candidate(n, day=randomizer.randint(1, 5), weeks=randomizer.sample(range(1, 5), 2), start=randomizer.randint(1, 3), end=4, credits=randomizer.choice([0.5, 1, 2, 3.25]), required=randomizer.random() < 0.15, code=str(randomizer.randint(1, 7)), campus=randomizer.choice(['east', 'west', ''])) for n in range(1, randomizer.randint(2, 10))]
            for item in items:
                item.preference = randomizer.randint(-3, 3)
            constraints = Constraints(min_credits=2, max_credits=8, target_credits=5, prefer_free_weekdays=[2, 4], limit=3, cross_campus_gap_periods=index % 3)
            expected = self.legacy(lambda: generate(items, constraints))
            self.assertEqual(generate(items, constraints), expected)

    def test_planner_bounds_required_unknown_and_search_cap(self):
        for items in [[candidate(1), candidate(1)], [candidate(1, required=True)], []]:
            if len(items) == 1:
                items[0].schedule_known = False
            expected = self.legacy(lambda: generate(items, Constraints()))
            self.assertEqual(generate(items, Constraints()), expected)
        a, b = candidate(1), candidate(2)
        a.semester_id, b.semester_id = 1, 2
        self.assertEqual(generate([a, b], Constraints()), self.legacy(lambda: generate([a, b], Constraints())))
        # Independent nonoverlapping weeks make the search bound reachable.
        items = [candidate(i, weeks=[i], credits=0.5) for i in range(1, 26)]
        expected = self.legacy(lambda: generate(items, Constraints(max_courses=25)))
        result = generate(items, Constraints(max_courses=25))
        self.assertEqual(result, expected)
        self.assertTrue(result['search_truncated'])
        self.assertFalse(result['optimality_proven'])

    def test_malformed_and_private_html_fail_closed(self):
        private = '<title>Test</title><script>PRIVATE_SCRIPT</script><input value="PRIVATE_FORM"><textarea>PRIVATE_DRAFT</textarea><a href="/home?ticket=PRIVATE_LINK">受限</a><a href="/home">首页</a>'
        result = self.call('home', text=private, max_chars=1000)
        self.assertNotIn('PRIVATE_', json.dumps(result))
        self.assertEqual(result['links'], [{'text': '首页', 'path': '/home'}])
        for action, arguments in [
            ('context', {'text': '<h1>changed</h1>', 'url': 'https://jw.ustc.edu.cn/home'}),
            ('json', {'text': 'PRIVATE_NOT_JSON'}),
            ('grade_selection', {'semesters': [], 'types': [], 'train_type_id': 1}),
            ('grades', {'data': {}}),
            ('window', {'data': {}}),
            ('validate_response', {'text': '<input type="password">'}),
            ('plan', {'candidates': [candidate(1).model_dump()], 'constraints': {'max_courses': True}}),
            ('plan', {'candidates': [{**candidate(1).model_dump(), 'source': 'invented'}], 'constraints': {}}),
        ]:
            with self.subTest(action=action), self.assertRaises(ModelError) as failure:
                self.call(action, **arguments)
            self.assertNotIn('PRIVATE_', str(failure.exception))

    def test_invalid_semester_and_week_stop_before_later_network(self):
        fixture = Fixture()
        bridge = self.response_bridge(lambda p, q, method: fixture.client.get(p, q))
        current = JWClient(cookies=[], user_agent='SyntheticBrowser/1')
        with patch('school_mcp.authenticated_native.request', side_effect=bridge) as request:
            with self.assertRaisesRegex(JWError, '学期'):
                current.courses(6)
            self.assertEqual(request.call_count, 1)
            request.reset_mock()
            with self.assertRaisesRegex(JWError, '周次'):
                current.timetable(week=3)
            self.assertEqual(request.call_count, 2)


if __name__ == '__main__':
    unittest.main()
