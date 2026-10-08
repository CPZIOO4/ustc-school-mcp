"""Frozen, account-bound submissions with a durable one-shot write gate."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ..mail.config import local_dir
from ..mail.credentials import _dpapi
from .assignments import catalog
from .identity import load_credentials
from .session import BBError, load_session
from .submission_browser import SubmissionBrowser
from .submission_contracts import baseline, file_record, require


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def identity():
    return digest(load_credentials()['username'])


def session_binding():
    return digest(load_session()['cookies'])


def now():
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, directory=None):
        self.path = (directory or local_dir()) / 'bb-submissions.sqlite3'

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, account TEXT, target TEXT, digest TEXT, state TEXT, phase TEXT, payload BLOB, result BLOB)')
            db.execute('CREATE TABLE IF NOT EXISTS target_locks (target TEXT PRIMARY KEY, job TEXT)')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except sqlite3.Error:
            db.rollback()
            raise BBError('本机提交记录不可用，已停止；请勿重复提交。') from None
        finally:
            db.close()

    def create(self, plan):
        identifier = uuid.uuid4().hex
        key = digest([plan['account'], plan['assignment']['course_id'], plan['assignment']['content_id']])
        with self.db() as db:
            duplicate = db.execute("SELECT id FROM jobs WHERE account=? AND digest=? AND state!='stopped_before_write'", (plan['account'], plan['sha256'])).fetchone()
            if duplicate:
                return duplicate[0]
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,NULL)',
                       (identifier, plan['account'], key, plan['sha256'], 'ready', 'prepared', _dpapi(json.dumps(plan).encode())))
        return identifier

    def get(self, identifier):
        require(bool(re.fullmatch(r'[0-9a-f]{32}', identifier)), '准备记录ID无效。')
        with self.db() as db:
            row = db.execute('SELECT account,state,phase,payload,result FROM jobs WHERE id=?', (identifier,)).fetchone()
        require(row and row[0] == identity(), '准备记录不存在或不属于当前账号。')
        return {'state': row[1], 'phase': row[2], 'plan': json.loads(_dpapi(row[3], decrypt=True)),
                'result': json.loads(_dpapi(row[4], decrypt=True)) if row[4] else None}

    def claim(self, identifier, sha256):
        account = identity()
        with self.db() as db:
            row = db.execute('SELECT account,target,digest,state FROM jobs WHERE id=?', (identifier,)).fetchone()
            require(row and row[0] == account and row[2] == sha256, '账号或材料摘要不匹配。')
            if row[3] != 'ready':
                return False
            require(not db.execute('SELECT job FROM target_locks WHERE target=?', (row[1],)).fetchone(),
                    '该作业已有执行中或结果待核实的提交；先查原记录，不创建另一份提交。')
            db.execute('INSERT INTO target_locks VALUES (?,?)', (row[1], identifier))
            db.execute("UPDATE jobs SET state='executing',phase='preflight' WHERE id=?", (identifier,))
        return True

    def advance(self, identifier, phase):
        with self.db() as db:
            row = db.execute('SELECT state,phase FROM jobs WHERE id=?', (identifier,)).fetchone()
            require(row and row[0] == 'executing', '提交执行状态发生变化，已停止。')
            require(row[1] != 'submit_requested', '最终提交已经请求，禁止重复发送。')
            db.execute('UPDATE jobs SET phase=? WHERE id=?', (phase, identifier))

    def finish(self, identifier, state, result, *, expected='executing'):
        with self.db() as db:
            row = db.execute('SELECT state FROM jobs WHERE id=?', (identifier,)).fetchone()
            if not row or row[0] != expected:
                return
            db.execute('UPDATE jobs SET state=?,result=? WHERE id=?',
                       (state, _dpapi(json.dumps(result, ensure_ascii=False).encode()), identifier))
            if state in {'verified', 'stopped_before_write'}:
                db.execute('DELETE FROM target_locks WHERE job=?', (identifier,))


def locate(course_id, content_id):
    listing = catalog(course_id, max_pages=15, limit=50)
    found = [a for a in listing['assignments'] if a['content_id'] == content_id]
    require(len(found) == 1, '当前课程中没有唯一匹配的作业，请重新定位。')
    return found[0]


def materials(files, *, browser, current, assignment, reuse_previous_files):
    require(bool(files) != reuse_previous_files, '指定本地文件与复用原附件必须二选一。')
    result = []
    total = 0
    entries = current['files'] if reuse_previous_files else files
    require(1 <= len(entries) <= 10, '仅支持1至10个附件。')
    for entry in entries:
        if reuse_previous_files:
            name = entry['filename']
            data = browser.download(entry['url'], assignment, current['attempt_id'])
        else:
            path = Path(entry)
            require(path.is_absolute() and path.is_file(), '附件必须是用户指定的现有绝对路径。')
            name = path.name
            with path.open('rb') as stream:
                data = stream.read(50 * 1024 * 1024 - total + 1)
        total += len(data)
        require(total <= 50 * 1024 * 1024, '附件总大小超过本机50MiB限制。')
        result.append({**file_record(name, data), 'data': base64.b64encode(data).decode()})
    require(len({f['filename'].casefold() for f in result}) == len(result), '附件重名，已停止。')
    return result


def prepare_submission(course_id, content_id, files=None, comment='', *, reuse_previous_files=False,
                       resubmit=False, allow_late=False, browser_factory=SubmissionBrowser, store=None):
    require(len(comment) <= 10000, '附言最多10000字符。')
    require(files is None or isinstance(files, list), 'files应为文件路径列表。')
    store = store or Store()
    assignment = locate(course_id, content_id)
    with browser_factory() as browser:
        current, _, _ = browser.view(assignment)
        require((current['kind'] == 'history') == resubmit, '已有提交请明确选择resubmit；首次提交不能使用重交模式。')
        require(not resubmit or current['new_url'], '作业没有提供重新提交入口。')
        require(not reuse_previous_files or resubmit, '复用原附件需要已有提交。')
        snapshots = materials(files or [], browser=browser, current=current, assignment=assignment,
                              reuse_previous_files=reuse_previous_files)
    plan = {'account': identity(), 'session_binding': session_binding(), 'assignment': assignment,
            'baseline': baseline(current), 'files': snapshots, 'comment': comment,
            'resubmit': resubmit, 'allow_late': allow_late}
    plan['sha256'] = digest(plan)
    plan['prepared_at'] = now().isoformat()
    identifier = store.create(plan)
    return submission_status(identifier, store=store)


def verify_files(browser, plan, current):
    require(current['kind'] == 'history' and current['attempt_id'] != plan['baseline']['attempt_id'],
            '未出现可区分于准备时的新尝试，结果待核实。')
    expected = [{k: f[k] for k in ('filename', 'size_bytes', 'sha256')} for f in plan['files']]
    require(len(current['files']) == len(expected), '新记录附件数量不匹配，结果待核实。')
    actual = [file_record(f['filename'], browser.download(f['url'], plan['assignment'], current['attempt_id']))
              for f in current['files']]
    require(sorted(actual, key=lambda f: f['filename']) == sorted(expected, key=lambda f: f['filename']),
            '新记录附件与准备材料不一致，结果待核实。')
    return {'verified': True, 'new_attempt_id': current['attempt_id'], 'submitted_at_text': current['attempt_time_text'],
            'late_label_present': current['late'], 'files_match': True, 'attachment_count': len(actual)}


def submission_status(identifier, *, verify=False, browser_factory=SubmissionBrowser, store=None):
    store = store or Store()
    job = store.get(identifier)
    plan = job['plan']
    if verify and job['state'] == 'uncertain' and job['phase'] == 'submit_requested':
        try:
            with browser_factory() as browser:
                current, _, _ = browser.view(plan['assignment'])
                result = verify_files(browser, plan, current)
            store.finish(identifier, 'verified', result, expected='uncertain')
            job = store.get(identifier)
        except Exception:
            pass  # Never turn a verification failure into a repeat write.
    due = plan['baseline']['due_at']
    late = now() > datetime.fromisoformat(due) if due else None
    return {'preparation_id': identifier, 'content_sha256': plan['sha256'], 'state': job['state'], 'phase': job['phase'],
            'assignment': {k: plan['assignment'][k] for k in ('course_id', 'content_id', 'title')},
            'files': [{k: f[k] for k in ('filename', 'size_bytes', 'sha256')} for f in plan['files']],
            'comment': plan['comment'], 'resubmit': plan['resubmit'], 'due_text': plan['baseline']['due_text'],
            'late_now': late, 'allow_late': plan['allow_late'], 'deadline_known': bool(due),
            'result': job['result'], 'can_execute': job['state'] == 'ready' and bool(due) and (not late or plan['allow_late']),
            'next_action': 'review_materials_and_authorization' if job['state'] == 'ready' else
                           'read_only_verify_or_manual_review' if job['state'] in {'uncertain', 'executing'} else 'done',
            'note': 'ready只表示材料已冻结；最终操作需用户授权。重复执行同一ID不重交；结果不明时查询，不能另建记录重试。'}


def submit_assignment(identifier, expected_sha256, *, browser_factory=SubmissionBrowser, store=None):
    store = store or Store()
    job = store.get(identifier)
    plan = job['plan']
    require(plan['sha256'] == expected_sha256, '材料摘要不匹配，请使用准备结果中的摘要。')
    if not store.claim(identifier, expected_sha256):
        return submission_status(identifier, store=store)
    try:
        require(now() - datetime.fromisoformat(plan['prepared_at']) < timedelta(hours=24), '准备记录已超过24小时，请重新准备。')
        require(plan['session_binding'] == session_binding(), '登录会话已变化，请重新准备以核实账号和目标。')
        canonical = {k: v for k, v in plan.items() if k not in {'sha256', 'prepared_at'}}
        require(digest(canonical) == expected_sha256, '准备记录内容校验失败。')
        for f in plan['files']:
            require(file_record(f['filename'], base64.b64decode(f['data'])) == {k: f[k] for k in ('filename', 'size_bytes', 'sha256')})
        current_assignment = locate(plan['assignment']['course_id'], plan['assignment']['content_id'])
        require(current_assignment == plan['assignment'], '作业要求或入口已改变，请重新准备。')
        with browser_factory() as browser:
            current, content, target = browser.view(plan['assignment'])
            require(baseline(current) == plan['baseline'], '已有提交或截止条件发生变化，请重新准备。')
            require(current['due_at'], '无法确定截止时间，暂不自动提交。')
            require(plan['allow_late'] or now() <= datetime.fromisoformat(current['due_at']), '作业已逾期，需要在用户知情下重新准备并允许迟交。')
            browser.send(plan, current, content, target, lambda phase: store.advance(identifier, phase))
            require(store.get(identifier)['phase'] == 'submit_requested', '未记录最终提交请求，不确认提交成功。')
            current, _, _ = browser.view(plan['assignment'])
            result = verify_files(browser, plan, current)
            store.finish(identifier, 'verified', result)
    except Exception as exc:
        phase = store.get(identifier)['phase']
        state = 'stopped_before_write' if phase == 'preflight' else 'uncertain'
        message = str(exc) if isinstance(exc, BBError) else '浏览器或网络操作未完成；已停止，不自动重复写入。'
        store.finish(identifier, state, {'verified': False, 'message': message})
    return submission_status(identifier, store=store)
