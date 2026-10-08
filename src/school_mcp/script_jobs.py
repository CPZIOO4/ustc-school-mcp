"""Bounded, private, deterministic background jobs. No agent or arbitrary code runner."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .mail.config import local_dir
from .mail.credentials import _dpapi
from .workflow_store import require, digest

TERMINAL = {'completed', 'review', 'failed', 'cancelled', 'expired'}
KINDS = {'mail_send', 'mail_watch', 'mail_archive', 'mail_replies', 'bb_submit', 'jw_watch'}


def now():
    return datetime.now(timezone.utc)


def failure_code(exc):
    # Stable codes guide smaller agents without exposing raw exception text or response bodies.
    message = str(exc)
    for words, code in [
        (('UIDVALIDITY', '邮件编号已改变'), 'mailbox_identity_changed'),
        (('归档规则已改变', '分类规则已经改变'), 'classification_rules_changed'),
        (('登录', '验证码', '认证'), 'authentication_required'),
        (('账号',), 'account_changed'),
        (('截止', '有效期', '超过24小时'), 'deadline_or_plan_expired'),
        (('截断', '超过5页'), 'narrow_search_required'),
        (('网络', '超时', '冷却'), 'network_unavailable'),
    ]:
        if any(word in message for word in words):
            return code
    return 'precondition_or_adapter_changed'


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        require(result.tzinfo is not None, '时间必须包含时区，例如 +08:00。')
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise ValueError('时间必须是带时区的 ISO 日期时间。') from None


def account(service):
    if service == 'mail':
        from .mail.config import load_config
        return digest(load_config().address.casefold())
    require(service in {'jw', 'bb'}, '不支持的脚本服务。')
    from .bb.identity import load_credentials
    return digest(load_credentials()['username'])


class JobStore:
    def __init__(self, directory=None):
        self.path = (directory or local_dir()) / 'script-jobs.sqlite3'

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, dedup TEXT UNIQUE, payload BLOB)')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        finally:
            db.close()

    def _get(self, db, identifier):
        require(isinstance(identifier, str) and re.fullmatch('[a-f0-9]{32}', identifier), '无效的脚本任务 ID。')
        row = db.execute('SELECT payload FROM jobs WHERE id=?', (identifier,)).fetchone()
        require(row is not None, '脚本任务不存在。')
        return json.loads(_dpapi(row[0], decrypt=True))

    def _put(self, db, identifier, value):
        value['updated_at'] = now().isoformat()
        db.execute('UPDATE jobs SET payload=? WHERE id=?', (_dpapi(json.dumps(value, ensure_ascii=False).encode()), identifier))

    def get(self, identifier):
        with self.db() as db:
            return self._get(db, identifier)

    def create(self, service, kind, parameters, start_at, end_at, poll_seconds, account_id):
        require(kind in KINDS and kind.startswith(service + '_'), '不支持的固定流程。')
        start, end = timestamp(start_at), timestamp(end_at)
        require(now() - timedelta(minutes=5) <= start <= now() + timedelta(days=30), '开始时间须在现在前5分钟至未来30天内。')
        require(start < end <= start + timedelta(days=30) and end > now(), '结束时间须晚于现在及开始时间，最长30天。')
        require(type(poll_seconds) is int and 60 <= poll_seconds <= 3600, '轮询间隔为60–3600秒。')
        spec = dict(service=service, kind=kind, parameters=parameters, start_at=start.isoformat(),
                    end_at=end.isoformat(), poll_seconds=poll_seconds, account=account_id)
        key = digest(spec)
        with self.db() as db:
            existing = db.execute('SELECT id FROM jobs WHERE dedup=?', (key,)).fetchone()
            if existing:
                return existing[0]
            active = []
            unresolved_archives = []
            for (payload,) in db.execute('SELECT payload FROM jobs'):
                other = json.loads(_dpapi(payload, decrypt=True))
                if other['account'] == account_id and other['service'] == service:
                    if other['state'] not in TERMINAL:
                        active.append(other)
                    if other['kind'] == 'mail_archive' and (other['effect_started'] or
                            other['result'].get('outcome') in {'archive_requires_review', 'operation_requires_review'}):
                        unresolved_archives.append(other)
            require(len(active) < 20, '本服务已有20个未结束任务，请先核查或取消。')
            if kind == 'mail_archive':
                require(not any(o['kind'] == kind and o['parameters']['mailbox'] == parameters['mailbox'] for o in active + unresolved_archives),
                        '该文件夹已有运行中或写入结果未核实的归档任务，先核查原任务，不能重复启动。')
            identifier = uuid.uuid4().hex
            spec.update(state='ready', claimed=False, cancel_requested=False, effect_started=False,
                        checkpoint={}, result={}, updated_at=now().isoformat(), worker=None)
            db.execute('INSERT INTO jobs VALUES (?,?,?)', (identifier, key, _dpapi(json.dumps(spec).encode())))
        return identifier

    def update(self, identifier, **values):
        with self.db() as db:
            job = self._get(db, identifier)
            job.update(values)
            self._put(db, identifier, job)

    def claim(self, identifier):
        with self.db() as db:
            job = self._get(db, identifier)
            if job['claimed'] or job['state'] in TERMINAL or job['cancel_requested']:
                return False
            job.update(claimed=True, state='waiting')
            self._put(db, identifier, job)
            return True

    def effect(self, identifier, checkpoint):
        """Commit operation reference BEFORE its first external write; cancellation shares the lock."""
        with self.db() as db:
            job = self._get(db, identifier)
            require(not job['cancel_requested'] and job['state'] == 'running', '任务已停止，不能开始写入。')
            require(not job['effect_started'], '已有未核实的写操作，不能重发。')
            require(timestamp(job['end_at']) > now(), '任务已到截止时间，不能开始写入。')
            job.update(effect_started=True, checkpoint=checkpoint)
            self._put(db, identifier, job)

    def enter(self, identifier):
        """A copied CLI invocation cannot join the same job, even during a read phase."""
        with self.db() as db:
            job = self._get(db, identifier)
            if not job['claimed'] or job.get('entered') or job['state'] in TERMINAL:
                return False
            job['entered'] = True
            self._put(db, identifier, job)
            return True

    def cancel(self, identifier):
        with self.db() as db:
            job = self._get(db, identifier)
            if job['state'] not in TERMINAL:
                job['cancel_requested'] = True
                if not job['effect_started']:
                    job['state'] = 'cancelled'
                self._put(db, identifier, job)
        return dict(cancel_requested=job['cancel_requested'], state=job['state'],
                    in_flight_may_complete=job['effect_started'])


def view(identifier, service, *, include_results=False, store=None):
    store = store or JobStore()
    job = store.get(identifier)
    require(job['service'] == service and job['account'] == account(service), '任务不属于当前服务或账号。')
    alive = False
    if job.get('worker'):
        from .login_runtime import running, process_started_at
        w = job['worker']
        alive = running(w['pid'])
        if alive and os.name == 'nt':
            alive = process_started_at(w['pid']) == w['created']
    interrupted = job['state'] not in TERMINAL and job['claimed'] and not alive
    result = dict(job_id=identifier, workflow=job['kind'], state=job['state'],
                  start_at=job['start_at'], end_at=job['end_at'], poll_seconds=job['poll_seconds'],
                  updated_at=job['updated_at'], worker_running=alive, interrupted=interrupted,
                  cancel_requested=job['cancel_requested'], in_flight_may_complete=job['effect_started'],
                  uses_ai=False, survives_reboot=False, retry_write_allowed=False,
                  next_action='inspect_original_operation_do_not_restart' if interrupted or job['state'] == 'review'
                  else 'done' if job['state'] in TERMINAL else 'query_job_status',
                  outcome=job['result'].get('outcome'), content_is_untrusted=True)
    result['reason_code'] = job['result'].get('reason_code')
    for key in ('processed', 'classification_counts', 'window', 'available_seats', 'requested_seats_available'):
        if key in job['result']:
            result[key] = job['result'][key]
    # Identifiers are sufficient to resolve an uncertain operation, without returning mail/course contents.
    result['operation'] = job['checkpoint'].get('operation')
    if include_results:
        result['result'] = job['result']
    return result


def launch(identifier, store=None):
    store = store or JobStore()
    # One launch claim survives process death; an interrupted write is never replayed.
    if not store.claim(identifier):
        return
    executable = Path(sys.executable)
    if os.name == 'nt':
        executable = executable.with_name('pythonw.exe')
    env = dict(os.environ, SCHOOL_MCP_BROWSER_HEADED='0', PYTHONUTF8='1')
    try:
        from .background_process import spawn
        process = spawn([str(executable), '-m', 'school_mcp', 'script-worker',
                                    '--job-id', identifier, '--private-dir', str(store.path.parent.resolve())],
                        cwd=str(Path(__file__).resolve().parents[2]), env=env)
        from .login_runtime import process_started_at
        store.update(identifier, worker=dict(pid=process.pid, created=process_started_at(process.pid)))
        # Retain/reap the detached child without blocking MCP or emitting ResourceWarning.
        threading.Thread(target=process.wait, daemon=True).start()
    except OSError:
        store.update(identifier, state='failed', result={'outcome': 'worker_launch_failed'})


def run(identifier, *, store=None, step=None, sleep=time.sleep, clock=now):
    """Only the fixed adapters execute business operations. A worker never invokes a model."""
    store = store or JobStore()
    if not store.enter(identifier):
        return
    if step is None:
        from .script_workflows import step
    failures = 0
    while True:
        job = store.get(identifier)
        if job['state'] in TERMINAL:
            return
        if job['cancel_requested']:
            store.update(identifier, state='review' if job['effect_started'] else 'cancelled')
            return
        if clock() >= timestamp(job['end_at']):
            store.update(identifier, state='expired', result={**job['result'], 'outcome': 'deadline_reached'})
            return
        if clock() < timestamp(job['start_at']):
            sleep(min(5, (timestamp(job['start_at']) - clock()).total_seconds()))
            continue
        try:
            matches = job['account'] == account(job['service'])
        except Exception:
            store.update(identifier, state='review', result={'outcome': 'account_unavailable'})
            return
        if not matches:
            store.update(identifier, state='review', result={'outcome': 'account_changed'})
            return
        if job['effect_started']:
            store.update(identifier, state='review', result={'outcome': 'inspect_original_operation'})
            return
        store.update(identifier, state='running')
        try:
            state, result, checkpoint = step(identifier, job, store)
            require(state in TERMINAL | {'waiting'}, '脚本返回了未知状态。')
            if store.get(identifier)['cancel_requested'] and state == 'waiting':
                state = 'cancelled'
            store.update(identifier, state=state, result=result, checkpoint=checkpoint, effect_started=False)
            failures = 0
        except Exception as exc:
            current = store.get(identifier)
            # Only known network failures before a write may retry, never arbitrary application errors.
            transient = isinstance(exc, (TimeoutError, ConnectionError)) or any(
                word in str(exc) for word in ('网络请求失败', '连接邮箱超时', '无法连接学校邮箱', '冷却', '请等待'))
            failures += 1
            if current['cancel_requested'] and not current['effect_started']:
                store.update(identifier, state='cancelled')
                return
            if current['effect_started'] or not transient or failures >= 3:
                store.update(identifier, state='review', result={'outcome': 'operation_requires_review' if current['effect_started']
                                                                else 'connection_or_precondition_failed',
                                                                'reason_code': failure_code(exc),
                                                                'error_type': type(exc).__name__})
                return
            store.update(identifier, state='waiting', result={'outcome': 'read_retry_backoff', 'failures': failures})
        if store.get(identifier)['state'] in TERMINAL:
            return
        delay = max(job['poll_seconds'], min(3600, job['poll_seconds'] * 2 ** failures))
        until = clock() + timedelta(seconds=delay)
        while clock() < until:
            current = store.get(identifier)
            if current['cancel_requested'] or current['state'] in TERMINAL or clock() >= timestamp(job['end_at']):
                break
            sleep(min(5, (until - clock()).total_seconds()))
