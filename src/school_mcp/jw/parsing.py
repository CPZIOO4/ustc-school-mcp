from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .session import JWError


def course_context(html: str, url: str) -> dict:
    match = re.fullmatch(r"/for-std/course-table/info/(\d+)", urlsplit(url).path)
    soup = BeautifulSoup(html, "html.parser")
    select = soup.select_one("select#allSemesters")
    if not match or select is None:
        raise JWError("教务课表页面结构已变化，无法识别当前学生与学期。")
    semesters = [{"id": int(o["value"]), "name": o.get_text(strip=True), "current": o.has_attr("selected")} for o in select.select('option[value]') if str(o["value"]).isdigit()]
    current = next((s["id"] for s in semesters if s["current"]), None)
    biz_type = re.search(r"bizTypeId\s*:\s*(\d+)", html)
    if current is None or biz_type is None:
        raise JWError("课表页面未提供有效的当前学期或学生业务类型。")
    return {"student_id": int(match.group(1)), "biz_type_id": int(biz_type.group(1)), "current_semester_id": current, "semesters": semesters}


def lesson_row(lesson: dict) -> dict:
    course = lesson.get("course") or {}
    teachers = []
    for assignment in lesson.get("teacherAssignmentList", []):
        person = assignment.get("person") or (assignment.get("teacher") or {}).get("person") or {}
        name = person.get("nameZh") or person.get("nameEn")
        if name and name not in teachers:
            teachers.append(name)
    return {"lesson_id": lesson.get("id"), "course_code": course.get("code"), "course_name": course.get("nameZh") or course.get("nameEn"),
            "class_code": lesson.get("code"), "credits": lesson.get("credits", course.get("credits")), "teachers": teachers,
            "campus": (lesson.get("campus") or {}).get("nameZh"), "teaching_class": lesson.get("nameZh"), "remark": lesson.get("remark")}


def activity_row(activity: dict, dates: dict | None = None) -> dict:
    fields = {"lessonId": "lesson_id", "lessonCode": "class_code", "courseCode": "course_code", "courseName": "course_name", "weeksArray": "weeks",
              "weeksStr": "weeks_text", "weekday": "weekday", "startUnit": "start_period", "endUnit": "end_period", "startDate": "start", "endDate": "end",
              "room": "room", "building": "building", "campus": "campus", "teachers": "teachers", "credits": "credits", "lessonRemark": "remark", "groupNo": "group"}
    result = {public: activity.get(key) for key, public in fields.items()}
    result["self_defined_dates"] = bool(activity.get("selfDefineDate"))
    if dates:
        result["date"] = dates.get(str(activity.get("weekday")))
    return result


def grade_rows(data: dict, keyword: str = "") -> list[dict]:
    rows = []
    for semester in data.get("semesters", []):
        for score in semester.get("scores", []):
            name = score.get("courseNameCh") or score.get("courseNameEn") or ""
            if keyword.casefold() not in name.casefold():
                continue
            # The official grade-sheet UI masks rows using canSee only.
            # gradeIsShow/show are not used to hide the displayed score or GPA.
            visible = score.get("canSee") is not False
            rows.append({"semester_id": semester.get("id"), "semester_name": score.get("semesterCh"), "course_name": name, "course_code": score.get("courseCode"),
                         "class_code": score.get("lessonCode"), "hours": score.get("total"), "credits": score.get("credits"), "visible": visible,
                         "score": score.get("scoreCh") if visible else None, "grade_point": score.get("gp") if visible else None, "passed": score.get("passed") if visible else None})
    return rows
