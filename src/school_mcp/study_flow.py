"""Resumable staged school reads. Each call owns and closes its IO resources.

The quick phase never reads submission history. Continuation reads one history
at a time; hosts may render the saved snapshot before starting the next step.
"""
from datetime import datetime, timedelta, timezone, date as Date
from urllib.parse import urlsplit, parse_qs
import threading
import time
import os
from .operation_records import Records, stamp
from .jw.client import JWClient
from .bb.client import BBClient
from .bb.assignments import catalog
from .bb.assignment_review import inspect_assignment
from .service_errors import ServiceError


def matches(items, key, keyword):
    norm = lambda x: ''.join(str(x).lower().split())
    exact = [x for x in items if norm(x.get(key, '')) == norm(keyword)]
    return exact or [x for x in items if norm(keyword) in norm(x.get(key, ''))]


def link(entry, label):
    path = entry.get('entry_path', '')
    p = urlsplit(path)
    q = parse_qs(p.query)
    if p.scheme or p.netloc or p.path != '/webapps/assignment/uploadAssignment' or '\\' in path or any(c in path for c in '\r\n'):
        return None
    if q.get('course_id') != [entry.get('course_id')] or q.get('content_id') != [entry.get('content_id')] or q.get('mode') != ['view']:
        return None
    return {'label': label, 'url': 'https://www.bb.ustc.edu.cn' + path, 'checked_at': stamp()}


def view(v):
    return {**{k: v[k] for k in ('date', 'state', 'timetable', 'assignments', 'updated_at', 'timings', 'error') if k in v},
            'query_id': v['id'], 'content_is_untrusted': True,
            'next_tool': 'school_study_continue' if any(a.get('state') == 'pending' for a in v['assignments']) else None,
            'note': 'pending 表示提交历史等待核实。已读数据与来源链接保存在本任务中，附件正文未读取。'}


def start(date, assignments, stopped=None, notify=None):
    stopped = stopped or threading.Event()
    notify = notify or (lambda *a: None)
    day = Date.fromisoformat(date)
    today = datetime.now(timezone(timedelta(hours=8))).date()
    if not 0 <= (day - today).days <= 6 or not isinstance(assignments, list) or len(assignments) > 4:
        raise ValueError('日期限未来七天，作业目标最多四项。')
    for a in assignments:
        if not isinstance(a, dict) or set(a) != {'course_keyword', 'term', 'assignment_keyword'} or any(not isinstance(v, str) or len(v) > 80 for v in a.values()) or not a['course_keyword'].strip() or not a['assignment_keyword'].strip():
            raise ValueError('请提供课程、学期和作业关键词。')
    store = Records('study')
    v = store.create({'date': date, 'state': 'reading', 'timetable': None, 'assignments': [], 'timings': []})
    def persist():
        return store.update(v['id'], **{k: v[k] for k in ('state', 'timetable', 'assignments', 'timings', 'error') if k in v})
    def read(label, fn, service='bb'):
        if stopped.is_set():
            raise ServiceError('查询已停止，已读结果保留。', code='interrupted')
        t = time.monotonic(); notify('study_read', label)
        try:
            try: return fn()
            except Exception as exc:
                if getattr(exc, 'code', '') not in ('authentication_required', 'identity_required'):
                    raise
                from .desktop.connections import connect
                restored=connect(service, stopped, notify, force=True)
                if not restored.get('connected'):
                    raise ServiceError(restored.get('message', '请在账号页恢复学校登录。'), code=restored.get('error_code', 'authentication_required'))
                return fn()
        finally: v['timings'].append({'stage': label, 'seconds': round(time.monotonic()-t, 3)})
    try:
        table = read('读取课表', lambda: JWClient().timetable(0, 0, day.isoweekday()), 'jw')
        week = table.get('current_week')
        if not isinstance(week, int) or week < 1:
            raise ValueError('教务未提供当前教学周。')
        week += (today.weekday() + (day-today).days) // 7
        rows = [x for x in table['activities'] if week in x.get('weeks', [])]
        v['timetable'] = dict(table, activities=rows, count=len(rows), week=week, date=date)
        persist(); notify('study_snapshot', view(store.read(v['id'])))
        courses, catalogs = {}, {}
        for target in assignments:
            key = (target['course_keyword'], target['term'])
            if key not in courses:
                courses[key] = read('定位课程', lambda: BBClient().courses(keyword=key[0], term=key[1]))
            found = courses[key]['courses']
            if len(found) != 1:
                v['assignments'].append({'state': 'needs_input', 'course_keyword': key[0], 'courses': found})
            else:
                c = found[0]; cid = c['course_id']
                if cid not in catalogs:
                    catalogs[cid] = read('读取作业目录', lambda: catalog(cid, max_pages=15, limit=50, client=BBClient()))
                listed = catalogs[cid]; selected = matches(listed['assignments'], 'title', target['assignment_keyword'])
                if len(selected) != 1 or listed.get('truncated'):
                    v['assignments'].append({'state': 'needs_input', 'course': c['title'], 'matches': selected, 'truncated': listed.get('truncated', False)})
                else:
                    entry = selected[0]
                    v['assignments'].append({'state': 'pending', 'course': c['title'], 'entry': entry,
                        'link': link(entry, c['title']+' · '+entry['title']), 'attachments_read': False})
            persist(); notify('study_snapshot', view(store.read(v['id'])))
        v['state'] = 'partial' if any(a['state']=='pending' for a in v['assignments']) else 'completed'
    except Exception as exc:
        v['state'] = 'paused' if stopped.is_set() else 'partial_error'
        v['error'] = {'code': getattr(exc, 'code', 'read_failed'), 'message': '部分读取未完成，已读取的课表和作业保留。'}
        persist()
        return dict(view(store.read(v['id'])), error=v['error'])
    return view(persist())


def advance(query_id, stopped=None, notify=None):
    stopped = stopped or threading.Event(); notify = notify or (lambda *a: None)
    store = Records('study')
    # A bounded lease prevents parallel callers reading the same slow page.
    with store.db() as db:
        v = store.load(db, query_id)
        lease = v.get('lease_until', 0)
        from .login_runtime import running, process_started_at
        pid=v.get('lease_pid',0); start=v.get('lease_process_started')
        same_process=running(pid) and (start is None or process_started_at(pid)==start)
        if lease > time.time() and (not pid or same_process):
            return dict(view(v), step_running=True)
        index = next((i for i,a in enumerate(v['assignments']) if a.get('state')=='pending'), None)
        if index is None: return view(v)
        v['lease_until'] = time.time()+900
        v['lease_pid']=os.getpid();v['lease_process_started']=process_started_at(os.getpid())
        store.save(db, v)
    row = v['assignments'][index]; entry = row['entry']; t = time.monotonic()
    def progress(stage, text):
        if stopped.is_set(): raise ServiceError('查询已停止。', code='interrupted')
        if Records('study').account != store.account: raise ServiceError('账号已变化。', code='identity_mismatch')
        notify(stage, text)
    try:
        progress('study_read', '正在核实 '+entry['title']+' 的提交记录；课表和目录已可查看。')
        row['verification'] = inspect_assignment(entry['course_id'], entry['content_id'], include_attachment_names=False, progress=progress)
        row['state'] = 'verified' if row['verification'].get('page_kind') == 'submission_history' else 'error'
        if row['state'] == 'error': row['error'] = {'code': 'unverified_page', 'message': '作业页面模板尚未确认，保留目录和链接。'}
        row['verified_at'] = stamp()
    except Exception as exc:
        row['state'] = 'pending' if stopped.is_set() else 'error'
        row['error'] = {'code': getattr(exc, 'code', 'read_failed'), 'message': '本项提交记录尚未核实，目录和链接已保留。'}
    v['timings'].append({'stage': '核实 '+entry['title'], 'seconds': round(time.monotonic()-t,3)})
    v['state'] = 'paused' if stopped.is_set() else 'partial' if any(a.get('state')=='pending' for a in v['assignments']) else 'completed_with_errors' if any(a.get('state')=='error' for a in v['assignments']) else 'completed'
    return view(store.update(query_id, assignments=v['assignments'], timings=v['timings'], state=v['state'], lease_until=0))


def status(query_id):
    return view(Records('study').read(query_id))
