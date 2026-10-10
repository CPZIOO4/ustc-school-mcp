"""Bounded course-identity, timetable and announcement evidence workflow.

Announcement prose is evidence for review, never a calendar entry inferred from
posting timestamps, deadlines or relative dates.
"""
from __future__ import annotations

import re
import unicodedata

from ..connection_flow import ReadSession

JW_SOURCE = 'https://jw.ustc.edu.cn/for-std/course-table'
LAB = re.compile(r'实验|上机|实践|实训')
TIMING = re.compile(r'实验|上机|实践|实训|调课|停课|补课|改期|取消|时间|地点|教室|周[一二三四五六日天]|星期|\d{1,2}[:：]\d{2}')
CHANGE = re.compile(r'调课|停课|补课|改期|取消|暂定|待定|另行通知')


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', str(value or '')).casefold() if c.isalnum())


def select_courses(rows, query, lesson_id=0):
    key = normalized(query)
    exact = [r for r in rows if key in {normalized(r.get(k)) for k in ('course_name', 'course_code', 'class_code')}]
    candidates = exact or [r for r in rows if key in normalized(r.get('course_name'))]
    return [r for r in candidates if r.get('lesson_id') == lesson_id] if lesson_id else candidates


def bb_term(semester):
    match = re.fullmatch(r'(20\d{2})年([春秋夏冬])季学期', semester.get('name', '').strip())
    return match[1] + {'春': 'SP', '秋': 'FA', '夏': 'SU', '冬': 'WI'}[match[2]] if match else None


def select_bb(rows, course, term):
    # USTC identifiers in titles look like CODE.SECTION(2026FA). Require
    # a bounded identifier, not a substring that also matches section .010.
    def contains_code(title, code):
        return bool(code and re.search(r'(?<![\w.])' + re.escape(code)
                    + r'(?=\.20\d{2}(?:FA|SP|SU|WI)(?![A-Za-z0-9])|[^\w.]|$)', title, re.I))
    rows = [r for r in rows if re.search(r'(?<![A-Za-z0-9])' + re.escape(term) + r'(?![A-Za-z0-9])', r.get('title', ''))]
    exact_class = [r for r in rows if contains_code(r.get('title', ''), course.get('class_code'))]
    # Without the official class code, name alone cannot prove section identity.
    suggestions = [r for r in rows if normalized(course['course_name']) in normalized(r.get('title'))]
    return exact_class, suggestions


def announcement_evidence(page):
    text = page.get('text', '')
    lines = text.splitlines()
    keep = set()
    for index, line in enumerate(lines):
        if TIMING.search(line):
            keep.update(range(max(0, index - 1), min(len(lines), index + 3)))
    excerpts = []
    for index in sorted(keep):
        if excerpts and index == excerpts[-1]['last_line'] + 1:
            excerpts[-1]['text'] += '\n' + lines[index]
            excerpts[-1]['last_line'] = index
        else:
            excerpts.append({'text': lines[index], 'last_line': index})
    # Retain context but put a hard bound on what the model receives.
    budget = 5000
    result = []
    trimmed = False
    for excerpt in excerpts:
        value = excerpt['text']
        if budget <= 0:
            trimmed = True
            break
        result.append({'excerpt': value[:budget], 'source': page.get('url'), 'requires_review': True})
        trimmed |= len(value) > budget
        budget -= len(value)
    return {'items': result, 'truncated': bool(page.get('text_truncated')) or trimmed,
            'change_notice_detected': bool(CHANGE.search(text)),
            'note': '公告原文尚未转换为上课时间；发布时间、作业截止时间和相对日期不能当作课程安排。'}


def course_schedule(query: str, semester_id: int = 0, week: int = 0, weekday: int = 0,
                    focus: str = 'all', lesson_id: int = 0, include_bb: bool = True,
                    login_authorized: bool = False, wait_seconds: int = 20) -> dict:
    if (not isinstance(query, str) or not normalized(query) or len(query) > 200
            or any(type(v) is not int or not 0 <= v <= 10000000 for v in (semester_id, week, weekday, lesson_id))
            or weekday > 7 or focus not in {'all', 'experiment'} or type(include_bb) is not bool):
        raise ValueError('提供课程名称/代码，非负学期、周次、课堂ID；weekday=0–7，focus=all/experiment。')
    session = ReadSession(login_authorized, wait_seconds)
    output = {'state': 'insufficient_evidence', 'content_is_untrusted': True, 'course': None,
              'scope': {'query': query, 'week': week, 'weekday': weekday, 'focus': focus,
                        'include_bb': include_bb},
              'sources': {}, 'timetable': [], 'announcements': [],
              'next_action': '证据不足，不能据此断言没有课程或实验；按 sources 中的状态继续。'}
    courses = session.read('jw', 'courses', {'semester_id': semester_id})
    output['sources']['jw_courses'] = source_status(courses)
    if not courses['completed']:
        return output
    semester = courses['data']['semester']
    output['semester'] = semester
    candidates = select_courses(courses['data']['courses'], query, lesson_id)
    if len(candidates) != 1:
        output.update(state='ambiguous' if candidates else 'insufficient_evidence',
                      candidates=[minimal_course(c) for c in candidates],
                      next_action='使用候选课程的名称或 lesson_id 再查；不要将另一门相近名称课程代入。')
        return output
    course = candidates[0]
    output['course'] = minimal_course(course)
    table = session.read('jw', 'timetable', {'semester_id': semester['id'], 'week': week, 'weekday': weekday})
    output['sources']['jw_timetable'] = source_status(table)
    if table['completed']:
        matches = [a for a in table['data']['activities'] if a.get('lesson_id') == course['lesson_id']]
        output['regular_timetable_count'] = len(matches)
        if focus == 'experiment':
            matches = [a for a in matches if LAB.search(' '.join(str(a.get(k) or '') for k in ('course_name', 'remark', 'group')))]
        output['timetable'] = [{**a, 'source': JW_SOURCE} for a in matches]
    if include_bb:
        term = bb_term(semester)
        if not term:
            output['sources']['bb'] = {'state': 'semester_mapping_required', 'completed': False}
        else:
            bb = session.read('bb', 'courses', {'term': term})
            output['sources']['bb'] = source_status(bb)
            if bb['completed']:
                exact, suggestions = select_bb(bb['data']['courses'], course, term)
                if len(exact) != 1:
                    output['sources']['bb'] = {'state': 'course_mapping_required', 'completed': False,
                                               'candidates': exact or suggestions}
                else:
                    output['bb_course'] = exact[0]
                    notice = session.read('bb', 'announcements', {'course_id': exact[0]['course_id'], 'max_chars': 24000})
                    output['sources']['bb'] = source_status(notice)
                    if notice['completed']:
                        evidence = announcement_evidence(notice['data'])
                        output['announcements'] = evidence.pop('items')
                        output['announcement_coverage'] = evidence
    complete = all(s['completed'] for s in output['sources'].values())
    review = bool(output['announcements'] or output.get('announcement_coverage', {}).get('change_notice_detected'))
    limited = output.get('announcement_coverage', {}).get('truncated', False)
    custom = any(a.get('self_defined_dates') or not (a.get('start') and a.get('end'))
                 or (week and not a.get('date')) for a in output['timetable'])
    if review or custom:
        output.update(state='ambiguous', next_action='先核对公告原文或自定义日期；公告不自动覆盖教务课表，未核实前不确定最终上课时间。')
    elif complete and output['timetable'] and not limited:
        output.update(state='confirmed', confirmation_scope='仅本次已读取范围的教务排课；不保证未适配的通知渠道没有调整。',
                      next_action='按课表原值回答时间和地点；week=0 时只报周次，不推算日期。')
    output['partial'] = not complete or limited
    if focus == 'experiment' and not output['timetable']:
        output['note'] = '没有识别到带实验/上机标识的教务条目；普通授课时段不能证明实验课时间。'
    return output


def minimal_course(course):
    return {key: course.get(key) for key in ('lesson_id', 'course_code', 'course_name', 'class_code')}


def source_status(result):
    return {key: result[key] for key in ('state', 'completed', 'next_action', 'status_tool', 'retry_after_seconds',
                                       'poll_after_seconds', 'login_recovery_attempted') if key in result}
