from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from school_mcp.jw.client import JWClient
from school_mcp.jw.session import JWError, load_session, save_session

COOKIES = [{"name": "SESSION", "value": "synthetic-jw-session", "domain": "jw.ustc.edu.cn", "path": "/", "secure": True}]
HOME = '<title>个人教务门户</title><div id="e-top-menu">课程</div>'
CONTEXT = '''<select id="allSemesters"><option value="4">测试春季</option>
<option value="5" selected>测试秋季</option></select><script>load({bizTypeId: 2});</script>'''
COURSES = {
    "lessons": [{"id": 11, "nameZh": "测试教学班", "code": "CLASS-1", "credits": 3,
                 "course": {"code": "COURSE-1", "nameZh": "量子物理"},
                 "teacherAssignmentList": [{"person": {"nameZh": "测试教师", "phone": "synthetic-private-phone"}}]}],
    "weekIndices": [1, 2], "currentWeek": 2,
    "oddWeekIndex2dayOfWeek2Date": {"2": {"1": "2026-09-07", "7": "2026-09-06"}},
}
TIMETABLE = {"studentTableVm": {"student": {"phone": "synthetic-student-phone"}, "activities": [
    {"lessonId": 11, "courseName": "量子物理", "weekday": 7, "weeksArray": [2], "startDate": "08:00", "endDate": "09:40", "room": "测试教室"},
    {"lessonId": 11, "courseName": "量子物理", "weekday": 1, "weeksArray": [1, 2], "startDate": "10:00", "endDate": "11:40"},
]}}
GRADES = {"overview": {"passedCredits": 6, "gpa": 3.5, "privateReportPassword": "synthetic-report-secret"},
          "info": {"studentId": "synthetic-private-id"}, "semesters": [{"id": 4, "scores": [
              {"courseNameCh": "量子物理", "scoreCh": "90", "gp": 4, "passed": True, "canSee": True, "gradeIsShow": False, "show": False},
              {"courseNameCh": "实验物理", "scoreCh": "synthetic-hidden-score", "gp": 3, "passed": True, "canSee": False, "gradeIsShow": True},
              {"courseNameCh": "理论物理", "scoreCh": "synthetic-hidden-score", "gp": 3, "passed": True, "canSee": False, "show": True},
          ]}]}


class Fixture:
    def __init__(self):
        self.calls = []
        self.client = JWClient(COOKIES, "SyntheticBrowser/1", httpx.MockTransport(self.handle))

    def handle(self, request):
        self.calls.append(request)
        assert request.method == "GET" and request.url.scheme == "https"
        assert request.url.host == "jw.ustc.edu.cn"
        path = request.url.path
        if path == "/home":
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text=HOME)
        if path == "/for-std/course-table":
            return httpx.Response(302, headers={"Location": "/for-std/course-table/info/73"})
        if path == "/for-std/course-table/info/73":
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text=CONTEXT)
        if path == "/for-std/course-table/get-data":
            assert request.url.params["dataId"] == "73" and request.url.params["bizTypeId"] == "2"
            return httpx.Response(200, json=COURSES)
        if path == "/for-std/course-table/semester/5/print-data/73":
            assert request.url.params["weekIndex"] == ""
            return httpx.Response(200, json=TIMETABLE)
        if path == "/for-std/grade/sheet/getSemesters":
            return httpx.Response(200, json=[{"id": 4, "nameZh": "测试春季"}])
        if path == "/for-std/grade/sheet/getGradeSheetTypes":
            return httpx.Response(200, json=[{"id": 1, "nameZh": "主修"}])
        if path == "/for-std/grade/sheet/getGradeList":
            assert request.url.params["trainTypeId"] == "1"
            return httpx.Response(200, json=GRADES if request.url.params["semesterIds"] == "4" else {"overview": {}, "semesters": []})
        raise AssertionError(f"Unexpected fixture route: {path}")


class JWRequestTests(unittest.TestCase):
    def test_home_omits_private_scripts_forms_and_token_links(self):
        html = HOME + '<script>synthetic-report-secret</script><input value="synthetic-input-secret"><textarea>synthetic-draft-secret</textarea><a href="/home?ticket=synthetic-ticket">隐藏链接</a><a href="/home">首页</a>'
        client = JWClient(COOKIES, "SyntheticBrowser/1", httpx.MockTransport(lambda _: httpx.Response(200, headers={"Content-Type": "text/html"}, text=html)))
        self.assertTrue(client.check()["connected"])
        result = client.home()
        self.assertNotIn("synthetic-", json.dumps(result))
        self.assertEqual(result["links"], [{"text": "首页", "path": "/home"}])

    def test_login_or_incomplete_portal_is_not_authenticated(self):
        for html in ('<input type="password">', '<a href="/ucas-sso/login">统一认证</a>', '<title>维护中</title>'):
            client = JWClient(COOKIES, transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={"Content-Type": "text/html"}, text=html)))
            with self.assertRaises(JWError):
                client.check()

    def test_auth_and_unsafe_redirects_are_never_followed(self):
        for target in ('https://id.ustc.edu.cn/login', 'http://jw.ustc.edu.cn/home', 'https://name:secret@jw.ustc.edu.cn/home', '/logout', '/home?action=delete', '/for-std/course-table/get-data?dataId=73&dataId=74', 'https://jw.ustc.edu.cn:invalid/home'):
            calls = []
            def handle(request):
                calls.append(request)
                return httpx.Response(302, headers={"Location": target})
            client = JWClient(COOKIES, transport=httpx.MockTransport(handle))
            with self.assertRaises(JWError):
                client.check()
            self.assertEqual(len(calls), 1)

    def test_unknown_or_mutating_get_requests_are_blocked_before_network(self):
        fixture = Fixture()
        for path, params in (("/for-std/course-select", {}), ("/for-std/course-take-query/charge", {}), ("https://evil.example/home", {}), ("/home", {"action": "delete"}), ("/for-std/course-table/get-data", {"dataId": "73&action=delete"})):
            with self.assertRaises(JWError):
                fixture.client.get(path, params)
        self.assertEqual(fixture.calls, [])

    def test_network_and_invalid_json_errors_do_not_expose_response_details(self):
        def fail(request):
            raise httpx.ConnectError("synthetic-private-network-detail", request=request)
        client = JWClient(COOKIES, transport=httpx.MockTransport(fail))
        with self.assertRaises(JWError) as failure:
            client.check()
        self.assertNotIn("synthetic-private", str(failure.exception))
        client = JWClient(COOKIES, transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={"Content-Type": "application/json"}, text="synthetic-private-invalid-json")))
        with self.assertRaisesRegex(JWError, "JSON") as failure:
            client.modules()
        self.assertNotIn("synthetic-private", str(failure.exception))


class JWDataTests(unittest.TestCase):
    def test_courses_derive_student_and_semester_from_current_portal(self):
        fixture = Fixture()
        result = fixture.client.courses(keyword="量子")
        self.assertEqual(result["semester"]["id"], 5)
        self.assertEqual(result["courses"][0]["course_name"], "量子物理")
        self.assertEqual(result["courses"][0]["teaching_class"], "测试教学班")
        self.assertEqual(result["courses"][0]["teachers"], ["测试教师"])
        self.assertNotIn("synthetic-private", json.dumps(result))
        self.assertEqual(fixture.calls[-1].url.params["semesterId"], "5")
        fixture.client.courses(semester_id=4)
        self.assertEqual(fixture.calls[-1].url.params["semesterId"], "4")

    def test_invalid_semester_never_reaches_course_data_endpoint(self):
        fixture = Fixture()
        for identifier in (True, -1, 10000001):
            with self.assertRaises(JWError):
                fixture.client.courses(identifier)
        self.assertEqual(fixture.calls, [])
        with self.assertRaisesRegex(JWError, "学期"):
            fixture.client.courses(6)
        self.assertFalse(any(r.url.path.endswith("get-data") for r in fixture.calls))

    def test_timetable_filters_weeks_and_uses_school_sunday_date(self):
        fixture = Fixture()
        result = fixture.client.timetable(week=2, weekday=7)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["activities"][0]["date"], "2026-09-06")
        self.assertEqual(result["activities"][0]["start"], "08:00")
        self.assertNotIn("synthetic-student-phone", json.dumps(result))
        self.assertEqual(fixture.client.timetable(week=1)["count"], 1)
        with self.assertRaisesRegex(JWError, "周次"):
            fixture.client.timetable(week=3)
        with self.assertRaisesRegex(JWError, "weekday"):
            fixture.client.timetable(weekday=8)

    def test_grades_mask_unpublished_scores_and_keep_provider_overview(self):
        fixture = Fixture()
        result = fixture.client.grades()
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["grades"][0]["score"], "90")
        for row in result["grades"][1:]:
            self.assertFalse(row["visible"])
            self.assertIsNone(row["score"])
            self.assertIsNone(row["grade_point"])
            self.assertIsNone(row["passed"])
        self.assertNotIn("synthetic-", json.dumps(result))
        filtered = fixture.client.grades(keyword="量子")
        self.assertEqual(filtered["count"], 1)
        self.assertEqual(filtered["overview"]["passedCredits"], 6)
        with self.assertRaisesRegex(JWError, "修读类型"):
            fixture.client.grades(train_type_id=5)

    def test_current_semester_can_have_no_published_grades(self):
        fixture = Fixture()
        result = fixture.client.grades(semester_id=5)
        self.assertEqual(result["count"], 0)
        self.assertEqual(fixture.calls[-1].url.params["semesterIds"], "5")
        with self.assertRaisesRegex(JWError, "学期"):
            fixture.client.grades(semester_id=6)


@unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
class JWSessionTests(unittest.TestCase):
    def test_session_roundtrip_is_encrypted_and_scoped(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SCHOOL_MCP_LOCAL_DIR": directory}):
            save_session([*COOKIES, {"name": "CASTGC", "value": "synthetic-central-secret", "domain": "id.ustc.edu.cn"}], "SyntheticBrowser/1")
            ciphertext = (Path(directory) / "jw.session.dpapi").read_bytes()
            self.assertNotIn(b"synthetic-jw-session", ciphertext)
            self.assertNotIn(b"synthetic-central-secret", ciphertext)
            self.assertEqual(load_session()["cookies"], COOKIES)
            self.assertEqual(load_session()["user_agent"], "SyntheticBrowser/1")

    def test_missing_corrupt_or_other_site_session_has_safe_error(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SCHOOL_MCP_LOCAL_DIR": directory}):
            with self.assertRaisesRegex(JWError, "尚未登录"):
                load_session()
            (Path(directory) / "jw.session.dpapi").write_bytes(b"synthetic-corrupt-session")
            with self.assertRaisesRegex(JWError, "尚未登录"):
                load_session()
            with self.assertRaisesRegex(JWError, "没有取得"):
                save_session([{"domain": "example.com", "name": "other", "value": "synthetic-other"}], "SyntheticBrowser/1")


if __name__ == "__main__":
    unittest.main()
