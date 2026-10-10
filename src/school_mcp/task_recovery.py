"""Read-only, account-scoped discovery of existing tasks; never replay a write."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone, timedelta
from typing import Any, Literal

from mcp.types import ToolAnnotations

from .mail.config import local_dir
from .mail.credentials import _dpapi
from .workflow_store import digest

SERVICES = {'bb', 'young', 'icourse', 'nan7'}
ID = re.compile(r'^[a-f0-9]{32}$')
STATES = {'ready', 'scheduled', 'monitoring', 'checking', 'submitting', 'executing',
          'uncertain', 'verified', 'stopped', 'stopped_before_write', 'expired', 'cancelled',
          'waiting', 'running', 'completed', 'review', 'failed'}
PHASES = {'prepared', 'preflight', 'submit_requested', 'readback', 'uploading',
          'new_attempt_requested', 'materials_verified', 'publish_requested',
          'cooldown', 'read_backoff', 'waiting_for_project', 'waiting_for_open', 'waiting_for_button'}
STATUS = {'bb': ('school_bb_submission_status', 'preparation_id'),
          'young': ('school_young_registration_status', 'job_id'),
          'icourse': ('school_icourse_review_status', 'plan_id'),
          'nan7': ('school_nan7_offer_status', 'plan_id')}


def account(service):
    if service == 'bb':
        from .bb.identity import load_credentials
        return digest(load_credentials()['username'])
    if service == 'young':
        from .bb.identity import load_credentials
        return hashlib.sha256(load_credentials()['username'].encode()).hexdigest()
    if service == 'icourse':
        from .icourse.session import load_session
        return digest(load_session()['user_id'])
    from .nan7.session import load_session
    session = load_session()
    return digest(session.get('account') or session['token'])


def decode(blob):
    if not isinstance(blob, bytes) or len(blob) > 100 * 1024 * 1024:
        raise ValueError('invalid record')
    value = json.loads(_dpapi(blob, decrypt=True))
    if not isinstance(value, dict):
        raise ValueError('invalid record')
    return value


def step(tool, **arguments):
    return {'tool': tool, 'arguments': arguments}


def pagination(offset, limit, more):
    return {'next_offset': offset + limit if more and offset + limit <= 10000 else None,
            'scan_limit_reached': bool(more and offset + limit > 10000)}


def summary(service, identifier, state, phase=None, kind='primary'):
    state = state if state in STATES else 'unknown'
    return dict(task_id=identifier, task_kind=kind, service=service, state=state,
                phase=phase if phase in PHASES else None, retry_write_allowed=False,
                next_step=step(f'school_{service}_tasks', task_id=identifier, task_kind=kind))


def parse_time(value):
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise ValueError('missing timezone')
    return date


def route(service, identifier, state, phase, payload, result):
    """Only known status tools are emitted. Stored content cannot choose a tool."""
    item = summary(service, identifier, state, phase)
    tool, key = STATUS[service]
    item['next_step'] = step(tool, **{key: identifier, 'verify': False})
    item.update(stage='needs_review', reason_code='unknown_state', terminal=False,
                execution_authorized=False, network_checked=False)
    current = datetime.now(timezone.utc)
    if state == 'ready':
        expired = (parse_time(payload['prepared_at']) + timedelta(hours=24) <= current
                   if service == 'bb' else parse_time(payload['expires_at']) <= current)
        item.update(stage='needs_input' if expired else 'prepared',
                    reason_code='preparation_expired' if expired else 'review_materials_and_existing_authorization')
        if service == 'bb' and not expired:
            due = payload['baseline'].get('due_at')
            if not due or (parse_time(due) < current and not payload.get('allow_late')):
                item.update(stage='needs_input', reason_code='deadline_unknown_or_late')
    elif state in {'uncertain', 'submitting', 'executing'}:
        can_verify = (service == 'bb' and state == 'uncertain' and phase == 'submit_requested'
                      or service == 'icourse' and (state == 'uncertain' or phase == 'readback'))
        if service == 'nan7':
            from .nan7.client import valid_offer_id
            can_verify = bool(valid_offer_id(result.get('offer_id')))
        if service == 'young':
            can_verify = bool(result.get('item_id') or (result.get('item') or {}).get('id'))
        item.update(stage='needs_verification', reason_code='outcome_unknown_do_not_replay')
        item['next_step'] = step(tool, **{key: identifier, 'verify': bool(can_verify)})
        item['manual_review_required'] = not can_verify
    elif state == 'verified':
        item.update(stage='completed', reason_code='previously_verified', terminal=True, next_step=None)
    elif state in {'cancelled', 'expired', 'stopped', 'stopped_before_write'}:
        item.update(stage='stopped', reason_code=state, terminal=True)
    elif service == 'young' and state in {'scheduled', 'monitoring', 'checking'}:
        phase_hint = result.get('phase')
        reason = phase_hint if phase_hint in PHASES else 'scheduler_requires_check'
        if state == 'scheduled' and parse_time(payload.get('monitor_from') or payload['opens_at']) > current:
            reason = 'not_due_yet_scheduler_requires_check'
        item.update(stage='waiting', reason_code=reason, scheduler_checked=False)
        # Bounded discovery: the registration record is authoritative for account ownership.
        schedules = local_dir() / 'young-schedules'
        candidates = []
        if schedules.exists():
            for index, path in enumerate(schedules.glob('*.json')):
                if index >= 200:
                    item['schedule_search_truncated'] = True
                    break
                if path.is_symlink() or not ID.fullmatch(path.stem) or path.stat().st_size > 65536:
                    continue
                spec = json.loads(path.read_text(encoding='utf-8'))
                if spec.get('job_id') == identifier:
                    candidates.append(path.stem)
        if len(candidates) == 1 and not item.get('schedule_search_truncated'):
            item['next_step'] = step('school_young_schedule_status', schedule_id=candidates[0])
        else:
            item.update(stage='needs_review', reason_code='schedule_missing_ambiguous_or_truncated',
                        manual_review_required=True)
    return item


def query(service, task_id=None, task_kind='primary', limit=10, offset=0):
    if (service not in SERVICES or task_kind not in {'primary', 'script'}
            or task_kind == 'script' and service != 'bb'
            or type(limit) is not int or not 1 <= limit <= 50
            or type(offset) is not int or not 0 <= offset <= 10000
            or task_id is not None and (not isinstance(task_id, str) or not ID.fullmatch(task_id))):
        raise ValueError('参数无效；script仅用于BB，limit为1–50，offset为0–10000，ID来自任务结果。')
    base = dict(service=service, task_kind=task_kind, network_checked=False,
                writes_performed=False, retry_write_allowed=False, content_is_untrusted=True)
    try:
        owner = account(service)
    except Exception:
        tool = 'school_icourse_setup_guide' if service == 'icourse' else f'school_{service}_status'
        return dict(base, state='identity_unavailable', items=[], next_step=step(tool),
                    note='本机身份缺失或不可核对；不表示学校登录已失效，也不表示没有任务。')
    filename = ('script-jobs.sqlite3' if task_kind == 'script' else
                {'bb':'bb-submissions.sqlite3', 'young':'young-registrations.sqlite3',
                 'icourse':'business-plans.sqlite3', 'nan7':'business-plans.sqlite3'}[service])
    path = local_dir() / filename
    if not path.exists():
        return dict(base, state='not_found' if task_id else 'completed', items=[], next_offset=None)
    if path.is_symlink():
        return dict(base, state='records_unavailable', items=[], next_offset=None)
    try:
        # No constructor that creates tables, no migration, no write lock.
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.execute('PRAGMA query_only=ON')
            if task_kind == 'script':
                return script_query(db, owner, base, task_id, limit, offset)
            table = 'plans' if service in {'icourse','nan7'} else 'jobs'
            where, args = 'account=?', [owner]
            if table == 'plans':
                where += ' AND service=?'; args.append(service)
            if task_id:
                where += ' AND id=?'; args.append(task_id)
                fields = "id,state," + ("NULL" if service == 'young' else 'phase') + ',payload,result'
            else:
                fields = "id,state," + ("NULL" if service == 'young' else 'phase')
            rows = db.execute(f'SELECT {fields} FROM {table} WHERE {where} ORDER BY rowid DESC LIMIT ? OFFSET ?',
                              (*args, 1 if task_id else limit + 1, 0 if task_id else offset)).fetchall()
            items = []
            for row in rows[:limit]:
                if not ID.fullmatch(row[0]):
                    raise ValueError('bad identifier')
                if task_id:
                    items.append(route(service, row[0], row[1], row[2], decode(row[3]), decode(row[4]) if row[4] else {}))
                else:
                    items.append(summary(service, *row))
            return dict(base, state='not_found' if task_id and not items else 'completed', items=items,
                        **pagination(offset, limit, not task_id and len(rows) > limit),
                        scope='current_account_local_records', details_checked=bool(task_id))
    except Exception:
        # No raw exception, SQL, ciphertext, user content or partial results escape.
        return dict(base, state='records_unavailable', items=[], next_offset=None,
                    note='本机记录不可读或结构已变化；保留原记录，不重建任务或重发。')


def script_query(db, owner, base, task_id, limit, offset):
    rows = db.execute('SELECT id,payload FROM jobs ' + ('WHERE id=? ' if task_id else '') +
                      'ORDER BY rowid DESC LIMIT ? OFFSET ?',
                      ((task_id,) if task_id else ()) + (1 if task_id else limit + 1, 0 if task_id else offset)).fetchall()
    items = []
    for identifier, blob in rows[:limit]:
        job = decode(blob)
        if job.get('service') != 'bb' or job.get('account') != owner or job.get('kind') != 'bb_submit':
            continue
        if not ID.fullmatch(identifier):
            raise ValueError('bad identifier')
        item = summary('bb', identifier, job['state'], kind='script')
        if task_id:
            item.update(stage='needs_review', reason_code='check_worker_and_original_operation',
                        next_step=step('school_bb_script_status', job_id=identifier), worker_checked=False)
            if job['state'] in {'ready', 'waiting', 'running'}:
                item.update(stage='waiting', reason_code='worker_requires_check')
                if parse_time(job['start_at']) > datetime.now(timezone.utc):
                    item['reason_code'] = 'not_due_yet_worker_requires_check'
            elif job['state'] == 'completed' and job.get('result', {}).get('outcome') == 'verified':
                item.update(stage='completed', reason_code='previously_verified')
            elif job['state'] in {'review', 'failed'} or job.get('effect_started'):
                item.update(stage='needs_verification', reason_code='check_original_operation_do_not_replay')
            elif job['state'] in {'cancelled', 'expired'}:
                item.update(stage='stopped', reason_code=job['state'])
            operation = job.get('checkpoint', {}).get('operation', {})
            preparation = operation.get('preparation_id') or job.get('parameters', {}).get('preparation_id')
            if preparation and ID.fullmatch(preparation):
                item['related_task'] = step('school_bb_tasks', task_id=preparation)
            item['write_may_have_started'] = bool(job.get('effect_started'))
            code = job.get('result', {}).get('reason_code')
            if code in {'authentication_required','account_changed','deadline_or_plan_expired',
                        'network_unavailable','precondition_or_adapter_changed'}:
                item['reason_code'] = code
        items.append(item)
    return dict(base, state='not_found' if task_id and not items else 'completed', items=items,
                **pagination(offset, limit, not task_id and len(rows) > limit),
                scope='current_account_bb_scripts', pagination='bounded_storage_scan',
                note='脚本旧存储需逐页过滤；本页为空但next_offset非空时仍有未检查页。')


def install(mcp, service):
    @mcp.tool(name=f'school_{service}_tasks', annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def tasks(task_id: str | None = None, task_kind: Literal['primary','script'] = 'primary',
              limit: int = 10, offset: int = 0) -> dict[str, Any]:
        """恢复当前账号已有任务：省略ID分页列记录，传ID返回阶段、停止原因与固定下一步工具参数。primary为本站业务；script仅用于BB定时提交，需另查一遍。只读本机，不联网、不重新执行、不授予提交权限。列表历史状态不是现场结果；先查详情再按next_step读原预览/核验回执/检查调度器。结果不明禁止重建任务；没有记录不等于网站没有提交。"""
        return query(service, task_id, task_kind, limit, offset)
