"""Observed read-only offering and enrolment-window endpoints."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit
import httpx
from bs4 import BeautifulSoup

from ..network import http_client, bounded_request
from ..workflow_store import require
from .client import JWClient, validate_id
from .parsing import lesson_row
from .session import BASE_URL, JWError


def schedule_slots(text, campus=''):
    """Fail closed for unfamiliar schedule text; Java Calendar Sunday=1."""
    slots = []
    for line in text.splitlines():
        match = re.fullmatch(r'\s*([\d,~\-]+)周\s+.*?[:：]\s*([1-7])\(([\d,]+)\)(?:\s+.*)?', line)
        if not match:
            return []
        weeks = []
        for item in match[1].split(','):
            ends = re.split('[~-]', item)
            if len(ends) > 2 or any(not p.isdigit() for p in ends):
                return []
            low, high = int(ends[0]), int(ends[-1])
            if not 1 <= low <= high <= 60:
                return []
            weeks.extend(range(low, high + 1))
        periods = sorted(set(map(int, match[3].split(','))))
        if not periods or periods[0] < 1 or periods[-1] > 20:
            return []
        # Keep disjoint periods disjoint, instead of inventing an occupied gap.
        for period in periods:
            slots.append(dict(weekday=(int(match[2]) + 5) % 7 + 1, start_period=period, end_period=period, weeks=sorted(set(weeks)), campus=campus or ''))
    return slots


class AcademicClient:
    def __init__(self, client=None):
        self.jw = client or JWClient()

    def request(self, path, params=None, *, method='GET'):
        entry = re.fullmatch(r'/for-std/(lesson-search|course-select)(?:/(?:index|turns)/\d+)?', path)
        search = re.fullmatch(r'/for-std/lesson-search/semester/\d+/search/\d+', path)
        turns = path == '/ws/for-std/course-select/open-turns'
        require((method == 'GET' and (entry or search)) or (method == 'POST' and turns), '未适配的教务请求。')
        allowed = {'courseNameZhLike', 'courseCodeLike', 'teacherNameLike', 'queryPage__'} if search else {'studentId', 'bizTypeId'} if turns else set()
        require(set(params or {}) <= allowed, '未适配的教务查询参数。')
        cookies = httpx.Cookies()
        for c in self.jw.cookies:
            cookies.set(c['name'], c['value'], domain=c['domain'], path=c.get('path', '/'))
        try:
            with http_client('jw', JWError, cookies=cookies, headers={'User-Agent': self.jw.user_agent}, transport=self.jw.transport, timeout=20, follow_redirects=False) as c:
                response = bounded_request(c,method,BASE_URL+path,maximum_bytes=5*1024*1024,error_type=JWError,**({'data':params} if method=='POST' else {'params':params}))
            if response.is_redirect and entry:
                p = urlsplit(urljoin(str(response.url), response.headers.get('location', '')))
                require(p.scheme == 'https' and p.netloc == 'jw.ustc.edu.cn' and not p.query and re.fullmatch(r'/for-std/'+entry[1]+r'/(index|turns)/\d+', p.path), '教务需要重新登录或入口已变化。')
                require(p.path != path and not re.search(r'/\d+$', path), '教务跳转异常。')
                return self.request(p.path)
            require(response.status_code == 200 and len(response.content) <= 5 * 1024 * 1024, '教务请求未成功；检查连接或稍后重试。')
            require(any(t in response.headers.get('content-type','') for t in ['application/json','text/html']), '教务响应格式不支持。')
            return response
        except httpx.HTTPError:
            raise JWError('教务网络请求失败；未执行选退课。') from None

    def offerings(self, semester_id, keyword, page=1, limit=20):
        validate_id(semester_id, 'semester_id')
        require(semester_id > 0 and isinstance(keyword,str) and 1 <= len(keyword.strip()) <= 100, '提供实时学期ID及1–100字课程关键词。')
        require(type(page) is int and 1 <= page <= 100 and type(limit) is int and 1 <= limit <= 50, '页码1–100，每页1–50。')
        response = self.request('/for-std/lesson-search')
        soup = BeautifulSoup(response.text, 'html.parser')
        require(any(o.get('value') == str(semester_id) for o in soup.select('#semester option')), '学期不在全校开课查询范围。')
        # This mapping is checked before interpreting the numeric weekdays.
        require(soup.select_one('#weekIndexs option[value="2"]') is not None and '星期一' in soup.select_one('#weekIndexs option[value="2"]').get_text(), '星期编码已变化，不能可靠排课。')
        student = re.fullmatch(r'/for-std/lesson-search/index/(\d+)', urlsplit(str(response.url)).path)
        require(student is not None, '全校开课查询结构已变化。')
        raw = self.request(f'/for-std/lesson-search/semester/{semester_id}/search/{student[1]}', {'courseNameZhLike':keyword.strip(), 'queryPage__':f'{page},{limit}'}).json()
        require(isinstance(raw,dict) and isinstance(raw.get('data'),list) and isinstance(raw.get('_page_'),dict), '开课查询格式已变化。')
        paging = raw['_page_']
        require(all(type(paging.get(k)) is int and paging[k]>=0 for k in ['currentPage','rowsPerPage','totalRows','totalPages']), '开课分页字段异常。')
        require(paging.get('currentPage') == page, '学校返回页码不符，不能把第一页当后续页。')
        rows = []
        for item in raw['data'][:limit]:
            row = lesson_row(item)
            timetable_text = ((item.get('scheduleText') or {}).get('dateTimePlacePersonText') or {}).get('textZh') or ''
            slots = schedule_slots(timetable_text, row['campus'])
            row.update(schedule_text=timetable_text, slots=slots, schedule_known=bool(slots), capacity=item.get('limitCount'), selected_count=item.get('selectedStdCount'), eligibility_verified=False)
            complete=bool(row['course_code'] and row['course_name'] and row['lesson_id'] is not None and isinstance(row['credits'],(int,float)))
            row['planner_candidate'] = dict(semester_id=semester_id,lesson_id=str(row['lesson_id']), course_code=row['course_code'], course_name=row['course_name'], credits=row['credits'], slots=slots, schedule_known=bool(slots), source='official') if complete else None
            row['missing_planning_metadata']=not complete
            rows.append(row)
        return dict(semester_id=semester_id, offerings=rows, page=page, server_page_size=paging.get('rowsPerPage'), total_rows=paging.get('totalRows'), next_page=page+1 if page < paging.get('totalPages',page) else None, truncated=len(raw['data'])>limit, source=BASE_URL+'/for-std/lesson-search', content_is_untrusted=True,
                    next_action='use_planner_candidate_or_refine_search', note='开课不等于可选；截断时缩小关键词，勿丢弃同页剩余候选。')

    def window(self):
        response = self.request('/for-std/course-select')
        student = re.search(r'studentId:\s*(\d+)', response.text)
        biz = re.search(r'bizTypeId:\s*(\d+)', response.text)
        require(student and biz and 'fetchOpenTurns' in response.text, '选课入口结构已变化。')
        turns = self.request('/ws/for-std/course-select/open-turns', {'studentId':student[1], 'bizTypeId':biz[1]}, method='POST').json()
        require(isinstance(turns,list), '选课批次结构已变化。')
        return dict(state='window_closed' if not turns else 'requires_live_contract', open_turns=[{k:t[k] for k in ('id','name','nameZh','allowEnter','beginTime','endTime') if k in t} for t in turns if isinstance(t,dict)], can_execute=False,
                    next_action='wait_for_open_window' if not turns else 'inspect_current_turn_before_implementation', mutation_performed=False,
                    note='常规选退课与个性化选课/放弃修读是不同流程；不互相替代。当前无经过验证的选退课执行合同。')

    def planning_context(self, semester_id):
        context,data=self.jw.course_data(semester_id)
        candidates=[]
        for item in data['lessons']:
            row=lesson_row(item)
            text=((item.get('scheduleText') or {}).get('dateTimePlacePersonText') or {}).get('textZh') or ''
            slots=schedule_slots(text,row['campus'])
            if not isinstance(row['credits'],(int,float)):
                return dict(state='needs_input',missing_fields=['official_course_credits'],next_action='verify_official_course_data',candidates=[])
            candidates.append(dict(semester_id=context['semester']['id'],lesson_id=str(row['lesson_id']),course_code=row['course_code'],course_name=row['course_name'],credits=row['credits'],slots=slots,schedule_known=bool(slots),required=True,source='official'))
        return dict(state='context_ready',semester=context['semester'],candidates=candidates,missing_schedule_ids=[c['lesson_id'] for c in candidates if not c['schedule_known']],
                    next_action='combine_with_offering_candidates_then_plan', note='已选课程默认必须保留；只在用户明确要求调整的情景方案中修改required，不执行真实退课。',content_is_untrusted=True)

    def prepare_change(self, action, semester_id, lesson_id):
        require(action in {'select','drop'}, 'action 只能 select 或 drop；放弃成绩/修读不属于本工具。')
        validate_id(lesson_id, 'lesson_id')
        require(lesson_id > 0 and semester_id > 0, '需实时结果中的学期和课堂ID。')
        courses = self.jw.courses(semester_id)
        selected = [r for r in courses['courses'] if r['lesson_id'] == lesson_id]
        window = self.window()
        missing = []
        if action == 'drop' and not selected:
            missing.append('target_not_in_selected_courses')
        if action == 'select' and selected:
            missing.append('target_already_selected')
        if window['state'] == 'window_closed':
            missing.append('selection_window_closed')
        missing.append('live_selection_contract_not_verified')
        preview = dict(action=action, semester=courses['semester'], lesson_id=lesson_id, target=selected[0] if selected else None,
                       credit_change=-selected[0]['credits'] if selected and action=='drop' and isinstance(selected[0]['credits'],(int,float)) else None,
                       consequences=['退课后原名额可能无法恢复。'] if action=='drop' else ['开课、无冲突不代表具备选课资格。'])
        return dict(state='blocked', preview=preview, blockers=missing, can_execute=False, mutation_performed=False,
                    next_action=window['next_action'], window=window,
                    workflow=['check_live_conditions','present_exact_change','execute_only_with_user_authorization_and_verified_contract','read_back_selected_courses'])
