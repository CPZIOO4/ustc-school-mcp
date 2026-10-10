from __future__ import annotations

import re
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from ..network import http_client
from ..service_errors import identity_redirect
from bs4 import BeautifulSoup

from .session import BASE_URL, HOST, JWError, load_session
from .parsing import activity_row, course_context, grade_rows, lesson_row

READ_PATHS = {"/", "/home", "/home/menu", "/for-std/course-table", "/for-std/course-table/get-data", "/for-std/grade/sheet", "/for-std/grade/sheet/getSemesters", "/for-std/grade/sheet/getGradeSheetTypes", "/for-std/grade/sheet/getGradeList"}


def validate_path(path: str, params: dict | None = None) -> None:
    if path not in READ_PATHS and not re.fullmatch(r"/for-std/course-table/(?:info/\d+|semester/\d+/print-data/\d+)", path):
        raise JWError("此教务页面尚未纳入读取接口。")
    allowed = {"/for-std/course-table/get-data": {"bizTypeId", "semesterId", "dataId"}, "/for-std/grade/sheet/getGradeList": {"trainTypeId", "semesterIds"}}
    keys = allowed.get(path, {"weekIndex"} if "/print-data/" in path else set())
    for key, value in (params or {}).items():
        text = str(value)
        if key not in keys or len(text) > 1000 or not (re.fullmatch(r"\d+(?:,\d+)*", text) or key in {"semesterIds", "weekIndex"} and text == ""):
            raise JWError("此教务请求包含不支持的操作参数。")


def validate_id(value: int, name: str):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 10000000:
        raise JWError(f"{name} 必须为工具结果中的非负整数。")


class JWClient:
    def __init__(self, cookies=None, user_agent=None, transport=None):
        session = load_session() if cookies is None else None
        self.cookies = session["cookies"] if session else cookies
        self.user_agent = session["user_agent"] if session else user_agent
        self.transport = transport

    def get(self, path: str, params: dict | None = None) -> httpx.Response:
        validate_path(path, params)
        cookies = httpx.Cookies()
        for cookie in self.cookies:
            cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie.get("path", "/"))
        try:
            with http_client("jw", JWError, cookies=cookies, timeout=20, follow_redirects=False, transport=self.transport, headers={"User-Agent": self.user_agent or "SchoolMCP/0.1"}) as client:
                url = BASE_URL + path
                for _ in range(5):
                    response = client.get(url, params=params)
                    params = None
                    if response.is_redirect:
                        target = urlsplit(urljoin(str(response.url), response.headers.get("location", "")))
                        if identity_redirect(target.geturl()) or (target.scheme == 'https' and target.hostname == HOST and target.port in (None, 443) and not target.username and not target.password and target.path in {'/login', '/ucas-sso/login'}):
                            raise JWError('教务系统需要认证。', code='authentication_required')
                        if target.scheme != "https" or target.hostname != HOST or target.port not in (None, 443) or target.username or target.password:
                            raise JWError("教务系统登录已失效，请调用 school_jw_reconnect 重新登录。")
                        query = parse_qsl(target.query, keep_blank_values=True)
                        if len(dict(query)) != len(query):
                            raise JWError("教务跳转包含重复参数。")
                        try:
                            validate_path(target.path, dict(query))
                        except JWError:
                            raise JWError("教务跳转不属于已支持的读取页面，或登录已失效；可调用 school_jw_reconnect 后再重试。") from None
                        url = BASE_URL + target.path
                        params = dict(query)
                        continue
                    if response.status_code in {401, 403}:
                        raise JWError("教务系统会话失效或没有访问权限。", code='authentication_required' if response.status_code == 401 else 'access_denied')
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if len(response.content) > 5 * 1024 * 1024 or not any(value in content_type for value in ("text/html", "application/json")):
                        raise JWError("教务页面类型或大小超出读取范围。")
                    soup = BeautifulSoup(response.text, "html.parser") if "text/html" in content_type else None
                    if soup and soup.select('a[href="/ucas-sso/login"], input[type="password"]'):
                        raise JWError("教务系统尚未认证。", code='authentication_required')
                    return response
                raise JWError("教务系统跳转次数超过限制。")
        except JWError:
            raise
        except (httpx.HTTPError, ValueError):
            raise JWError("无法读取教务系统，请检查网络与登录状态。") from None

    def home(self, max_chars=6000) -> dict:
        if not 1 <= max_chars <= 100000:
            raise JWError("max_chars 范围为 1–100000。")
        response = self.get("/home")
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else "教务系统"
        for node in soup.select("script, style, input, textarea, select"):
            node.decompose()
        links = []
        seen = set()
        for anchor in soup.select("a[href]"):
            text = anchor.get_text(" ", strip=True)
            parsed = urlsplit(urljoin(BASE_URL, anchor["href"]))
            if not text or not parsed.path or parsed.scheme != "https" or parsed.hostname != HOST or parsed.username or parsed.password or parsed.query or "logout" in parsed.path or parsed.path.startswith(("/login", "/ucas-sso")):
                continue
            key = (text, parsed.path)
            if key not in seen:
                seen.add(key)
                links.append({"text": text, "path": parsed.path})
        text = soup.get_text("\n", strip=True)
        return {"title": title, "text": text[:max_chars], "text_truncated": len(text) > max_chars, "links": links, "source": BASE_URL + "/home", "content_is_untrusted": True}

    def check(self) -> dict:
        response = self.get("/home")
        soup = BeautifulSoup(response.text, "html.parser")
        if soup.select_one("#e-top-menu") is None:
            raise JWError("未取得完整的个人教务门户，请重新登录或稍后重试。")
        return {"connected": True, "title": soup.title.get_text(strip=True) if soup.title else "教务系统", "read_only": True}

    def json(self, path: str, params: dict | None = None):
        response = self.get(path, params)
        try:
            return response.json()
        except ValueError:
            raise JWError("教务接口未返回有效 JSON，可能需要重新登录。") from None

    def modules(self) -> dict:
        data = self.json("/home/menu")
        if not isinstance(data, list):
            raise JWError("教务菜单结构已变化。")
        modules = []
        for item in data:
            path = item.get("href")
            if path and not re.fullmatch(r"/[A-Za-z0-9_/-]+", path):
                path = None
            modules.append({"id": item.get("id"), "parent_id": item.get("parentId"), "name": item.get("title"), "path": path, "supported_reading": path in READ_PATHS})
        return {"modules": modules, "count": len(modules), "content_is_untrusted": True}

    def context(self) -> dict:
        response = self.get("/for-std/course-table")
        return course_context(response.text, str(response.url))

    def semesters(self) -> dict:
        context = self.context()
        return {"semesters": context["semesters"], "current_semester_id": context["current_semester_id"], "content_is_untrusted": True}

    def course_data(self, semester_id: int = 0) -> tuple[dict, dict]:
        validate_id(semester_id, "semester_id")
        context = self.context()
        selected = semester_id or context["current_semester_id"]
        semester = next((s for s in context["semesters"] if s["id"] == selected), None)
        if semester is None:
            raise JWError("学期不在教务系统提供的列表中，请先调用 school_jw_list_semesters。")
        data = self.json("/for-std/course-table/get-data", {"bizTypeId": context["biz_type_id"], "semesterId": selected, "dataId": context["student_id"]})
        if not isinstance(data, dict) or not isinstance(data.get("lessons"), list):
            raise JWError("课程列表结构已变化。")
        context.update(semester=semester)
        return context, data

    def courses(self, semester_id: int = 0, keyword: str = "") -> dict:
        if len(keyword) > 200:
            raise JWError("课程关键词过长。")
        context, data = self.course_data(semester_id)
        rows = [lesson_row(lesson) for lesson in data["lessons"]]
        rows = [row for row in rows if keyword.casefold() in (row["course_name"] or "").casefold()]
        return {"semester": context["semester"], "courses": rows, "count": len(rows), "total_in_semester": len(data["lessons"]), "content_is_untrusted": True}

    def timetable(self, semester_id: int = 0, week: int = 0, weekday: int = 0) -> dict:
        validate_id(week, "week")
        validate_id(weekday, "weekday")
        if weekday > 7:
            raise JWError("weekday 为 0 或 1–7，1 表示周一，7 表示周日。")
        context, data = self.course_data(semester_id)
        if week and week not in data.get("weekIndices", []):
            raise JWError("周次不在该学期课表提供的范围内。")
        selected = context["semester"]["id"]
        result = self.json(f"/for-std/course-table/semester/{selected}/print-data/{context['student_id']}", {"weekIndex": ""})
        vm = result.get("studentTableVm") if isinstance(result, dict) else None
        if not isinstance(vm, dict) or not isinstance(vm.get("activities"), list):
            raise JWError("课表结构已变化。")
        dates = data.get("oddWeekIndex2dayOfWeek2Date", {}).get(str(week), {}) if week else {}
        rows = [activity_row(a, dates) for a in vm["activities"] if (not week or week in a.get("weeksArray", [])) and (not weekday or weekday == a.get("weekday"))]
        return {"semester": context["semester"], "week": week, "weekday": weekday, "current_week": data.get("currentWeek"), "activities": rows, "count": len(rows), "week_dates": dates, "content_is_untrusted": True, "note": "按教务平台的周次和星期编号返回安排；可能包含不同教学分组。"}

    def grades(self, semester_id: int = 0, train_type_id: int = 1, keyword: str = "", summary_only: bool = False) -> dict:
        validate_id(semester_id, "semester_id")
        validate_id(train_type_id, "train_type_id")
        if len(keyword) > 200:
            raise JWError("成绩课程关键词过长。")
        semesters = self.json("/for-std/grade/sheet/getSemesters")
        types = self.json("/for-std/grade/sheet/getGradeSheetTypes")
        if not isinstance(semesters, list) or not isinstance(types, list):
            raise JWError("成绩学期或修读类型结构已变化。")
        if train_type_id not in {t["id"] for t in types}:
            raise JWError("修读类型不在当前用户可查询的成绩单类型中。")
        identifiers = [s["id"] for s in semesters]
        if semester_id:
            if semester_id not in identifiers and semester_id not in {s["id"] for s in self.semesters()["semesters"]}:
                raise JWError("成绩学期不在教务系统提供的列表中。")
            identifiers = [semester_id]
        data = self.json("/for-std/grade/sheet/getGradeList", {"trainTypeId": train_type_id, "semesterIds": ",".join(map(str, identifiers))})
        if not isinstance(data, dict) or not isinstance(data.get("semesters"), list):
            raise JWError("成绩接口结构已变化。")
        rows = grade_rows(data, keyword)
        overview = {key: data.get("overview", {}).get(key) for key in ("passedCredits", "gpa", "weightedScore", "notPassedCredits", "arithmeticScore")}
        return {"train_type_id": train_type_id, "semesters": [{"id": s["id"], "name": s.get("nameZh")} for s in semesters if s["id"] in identifiers], "grades": [] if summary_only else rows, "details_omitted": summary_only, "count": len(rows), "overview": overview, "overview_scope": "所请求学期的完整成绩，关键词仅筛选课程行。", "content_is_untrusted": True}
