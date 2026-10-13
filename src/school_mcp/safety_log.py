"""Local, bounded diagnostic events. Never accept exception prose or business data.

This is a support log, not a tamper-proof security audit. Only allowlisted fields
are persisted and exported; no requests, URLs, locals, source lines or identifiers
from school records. Logging failure must never replay or replace an operation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import threading
import uuid
from contextlib import contextmanager
from functools import lru_cache

MAX_EVENTS = 500
RETENTION_SECONDS = 14 * 86400
MAX_DATABASE_BYTES = 8 * 1024 * 1024
STAGES = {'startup', 'api', 'model', 'tool', 'login', 'connection', 'document',
          'persistence', 'background', 'recovery', 'ui'}
SERVICES = {'app', 'agent', 'model', 'mail', 'bb', 'jw', 'young', 'finance',
            'library', 'teach', 'icourse', 'nan7', 'portal', 'student', 'spaces',
            'drive', 'vlab', 'web', 'digest'}
OUTCOMES = {
    'not_started': '操作尚未开始。',
    'read_failed': '本次查询未完成。',
    'local_failed': '本机处理未完成。',
    'unknown': '操作可能已产生效果，先查询原回执，不重复提交。',
    'interrupted': '任务中断，先核实原任务再继续。',
}
EXCEPTIONS = {'Exception', 'ValueError', 'TypeError', 'KeyError', 'OSError',
              'PermissionError', 'FileNotFoundError', 'TimeoutError', 'RuntimeError',
              'ConnectError', 'ConnectTimeout', 'ReadTimeout', 'HTTPStatusError',
              'ValidationError', 'ModelError', 'ServiceError', 'ToolError',
              'OperationalError', 'DatabaseError', 'Error'}
MODEL_CODES = {'invalid_tool_arguments', 'invalid_model_response', 'empty_response',
               'model_refusal', 'incomplete_model_response', 'model_rate_limited',
               'model_authentication_failed', 'model_unavailable', 'model_timeout',
               'model_connection_failed', 'tool_call_unsupported', 'model_error',
               'startup_failed', 'storage_failed', 'interrupted', 'ui_error'}
MODEL_CODES |= {'model_list_invalid', 'model_connection', 'context_capacity', 'model_tool_roundtrip',
                'model_auth', 'model_forbidden', 'model_not_found', 'model_request_rejected',
                'model_incompatible', 'model_authentication', 'model_balance', 'model_http_error'}
MODEL_CODES |= {'missing_file', 'unsupported_format', 'file_too_large', 'unreadable_file', 'text_too_large'}
MODEL_CODES |= {'storage_failure', 'storage_capacity', 'unsupported_platform', 'model_settings_changed', 'page_changed', 'response_too_large'}
_PACKAGE = Path(__file__).resolve().parent
_LOCK = threading.RLock()


@lru_cache(maxsize=1)
def _manifest():
    if not getattr(sys, 'frozen', False):
        return {}
    try:
        return json.loads((_PACKAGE / 'diagnostic-build.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _choice(value, choices, default):
    return value if isinstance(value, str) and value in choices else default


@lru_cache(maxsize=1)
def _tool_names():
    from .agent.catalog import WORKFLOWS
    return {name for flow in WORKFLOWS.values() for name in flow['tools']} | {
        'school_agent_call', 'school_agent_start', 'school_agent_tools',
        'school_agent_route', 'school_agent_task', 'school_agent_tasks',
        'school_local_list_files', 'school_local_read_file', 'school_local_create_file'}


@lru_cache(maxsize=1)
def _files():
    return ({p.relative_to(_PACKAGE).as_posix() for p in _PACKAGE.rglob('*.py')} |
            {p for p in _manifest().get('files', []) if isinstance(p, str) and re.fullmatch(r'[a-z_]+(?:/[a-z_]+)*\.py', p)})


@lru_cache(maxsize=1)
def _build():
    try:
        if getattr(sys, 'frozen', False):
            return _manifest().get('fingerprint', 'unavailable')
        from .runtime_version import MONITOR
        return MONITOR.startup['fingerprint']
    except Exception:
        return 'unavailable'


def directory():
    from .mail.config import local_dir
    return Path(os.environ['SCHOOL_MCP_DIAGNOSTICS_DIR']) if os.environ.get('SCHOOL_MCP_DIAGNOSTICS_DIR') else local_dir() / 'diagnostics'


def desktop_directory(root):
    # Separate from profile: startup/credential failures remain diagnosable.
    return Path(root) / 'diagnostics'


def configure_desktop(root):
    os.environ['SCHOOL_MCP_DIAGNOSTICS_DIR'] = str(desktop_directory(root))


def _frames(exc):
    output, seen = [], set()
    while exc is not None and id(exc) not in seen and len(seen) < 8:
        seen.add(id(exc))
        tb = exc.__traceback__
        while tb is not None:
            try:
                if getattr(sys, 'frozen', False):
                    module = tb.tb_frame.f_globals.get('__name__', '')
                    relative = module.removeprefix('school_mcp.').replace('.', '/') + '.py' if module.startswith('school_mcp.') else ''
                else:
                    relative = Path(tb.tb_frame.f_code.co_filename).resolve().relative_to(_PACKAGE).as_posix()
                if relative in _files():
                    output.append({'file': relative, 'line': min(tb.tb_lineno, 100000)})
            except (ValueError, OSError):
                pass
            tb = tb.tb_next
        exc = exc.__cause__
    return output[-8:]


def _clean(event):
    """Used on write AND export: even a manually edited log cannot export prose."""
    from .recovery import REASONS
    def number(name, maximum, default=0):
        n = event.get(name)
        return min(maximum, max(0, n)) if type(n) is int else default
    error_id = event.get('error_id', '')
    if not isinstance(error_id, str) or not re.fullmatch('ERR-[A-F0-9]{16}', error_id):
        error_id = 'ERR-' + uuid.uuid4().hex[:16].upper()
    frames = []
    for f in (event.get('frames') if isinstance(event.get('frames'), list) else [])[:8]:
        if isinstance(f, dict) and isinstance(f.get('file'), str) and f['file'] in _files() and type(f.get('line')) is int:
            frames.append({'file': f['file'], 'line': min(100000, max(1, f['line']))})
    build = event.get('build', '')
    return dict(error_id=error_id, code=_choice(event.get('code'), set(REASONS) | MODEL_CODES, 'unavailable'),
                service=_choice(event.get('service'), SERVICES, 'app'),
                stage=_choice(event.get('stage'), STAGES, 'tool'),
                outcome=_choice(event.get('outcome'), OUTCOMES, 'unknown'),
                tool=_choice(event.get('tool'), _tool_names(), ''),
                exception=_choice(event.get('exception'), EXCEPTIONS, 'Exception'),
                frames=frames, first_at=number('first_at', 4102444800), last_at=number('last_at', 4102444800),
                count=number('count', 1000000, 1), duration_ms=number('duration_ms', 86400000),
                build=build if isinstance(build, str) and re.fullmatch('[a-f0-9]{64}', build) else 'unavailable',
                platform=_choice(event.get('platform'), {'win32', 'linux', 'darwin'}, 'other'))


@contextmanager
def _storage_lock(root=None):
    root = Path(root) if root is not None else directory()
    if root.is_symlink():
        raise OSError('diagnostic directory is redirected')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = root / 'events.lock'
    if lock.is_symlink():
        raise OSError('diagnostic lock is redirected')
    with _LOCK, lock.open('a+b') as handle:
        if lock.stat().st_size == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            deadline = time.monotonic()+.6
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline: raise
                    time.sleep(.02)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield root
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def _db(root=None):
    with _storage_lock(root) as directory_path:
        with _opened_db(directory_path) as db:
            yield db


@contextmanager
def _opened_db(root):
    path = root / 'events.sqlite3'
    if any((root/n).is_symlink() for n in ('events.sqlite3', 'events.sqlite3-journal', 'events.sqlite3-wal', 'events.sqlite3-shm')) or path.exists() and path.stat().st_size > MAX_DATABASE_BYTES:
        raise OSError('diagnostic database unavailable')
    db = sqlite3.connect(path, timeout=.5)
    try:
        if os.name != 'nt':
            path.chmod(0o600)
        db.execute('PRAGMA secure_delete=ON')
        db.execute('PRAGMA journal_mode=DELETE')
        db.execute('PRAGMA max_page_count=2048')
        db.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, signature TEXT, last_at INTEGER, payload TEXT)')
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM events WHERE last_at < ?', (int(time.time()) - RETENTION_SECONDS,))
        yield db
        db.commit()
    finally:
        db.close()


def record(*, code='unavailable', service='app', stage='tool', tool='', outcome='unknown', exc=None, duration_ms=0, root=None):
    """Return an error ID even if the disk is full. No stdout/stderr fallback."""
    identifier = 'ERR-' + uuid.uuid4().hex[:16].upper()
    try:
        now = int(time.time())
        event = _clean(dict(error_id=identifier, code=code, service=service, stage=stage, tool=tool,
                            outcome=outcome, exception=type(exc).__name__ if exc is not None else 'Exception',
                            frames=_frames(exc), first_at=now, last_at=now, count=1,
                            duration_ms=duration_ms, build=_build(), platform=sys.platform))
        signature = hashlib.sha256(json.dumps({k: event[k] for k in
            ('code', 'service', 'stage', 'tool', 'outcome', 'exception', 'frames', 'build')}, sort_keys=True).encode()).hexdigest()
        with _db(root) as db:
            row = db.execute('SELECT payload FROM events WHERE signature=? AND last_at>=? ORDER BY last_at DESC LIMIT 1',
                             (signature, now - 300)).fetchone()
            if row:
                previous = _clean(json.loads(row[0]))
                event.update(error_id=previous['error_id'], first_at=previous['first_at'], count=min(1000000, previous['count'] + 1))
            db.execute('INSERT OR REPLACE INTO events VALUES (?,?,?,?)',
                       (event['error_id'], signature, now, json.dumps(event, ensure_ascii=True)))
            db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY last_at DESC, rowid DESC LIMIT ?)', (MAX_EVENTS,))
        return {'error_id': event['error_id'], 'diagnostic_saved': True}
    except Exception:
        return {'error_id': identifier, 'diagnostic_saved': False,
                'diagnostic_note': '诊断日志无法写入，请检查磁盘空间和目录权限；本次错误编号仅供当前界面使用。'}


def attach(problem, *, service='app', stage='tool', tool='', exc=None, outcome=None, duration_ms=0, root=None):
    # Preserve a preceding layer's ID instead of producing a second incident.
    if isinstance(problem.get('error_id'), str) and re.fullmatch('ERR-[A-F0-9]{16}', problem['error_id']):
        return problem
    current, seen = exc, set()
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        previous = getattr(current, '_school_diagnostic', None)
        if (isinstance(previous, dict) and isinstance(previous.get('error_id'), str)
                and re.fullmatch('ERR-[A-F0-9]{16}', previous['error_id']) and type(previous.get('diagnostic_saved')) is bool):
            return {**problem, **{k: previous[k] for k in ('error_id', 'diagnostic_saved')}}
        current = current.__cause__
    outcome = outcome or ('unknown' if problem.get('recovery', {}).get('action') == 'verify_original_receipt' else 'read_failed')
    return {**problem, **record(code=problem.get('error_code'), service=service, stage=stage, tool=tool,
                              exc=exc, outcome=outcome, duration_ms=duration_ms, root=root)}


def report(root=None):
    try:
        with _db(root) as db:
            rows = db.execute('SELECT payload FROM events ORDER BY last_at DESC, rowid DESC LIMIT ?', (MAX_EVENTS,)).fetchall()
        events = [_clean(json.loads(row[0])) for row in rows]
        for e in events:
            e['outcome_note'] = OUTCOMES[e['outcome']]
        return dict(schema_version=1, available=True, generated_at=int(time.time()),
                    retention_days=14, max_events=MAX_EVENTS, events=events,
                    privacy='仅含固定诊断字段；不含账号、凭据、网址、请求正文、对话、附件或截图。',
                    automatic_upload=False)
    except Exception:
        return dict(schema_version=1, available=False, events=[], automatic_upload=False,
                    note='诊断记录无法读取，请检查磁盘、目录权限或日志文件是否损坏。')


def summary(root=None):
    value = report(root)
    events = value['events']
    return dict(available=value['available'], incident_count=len(events),
                occurrence_count=sum(e['count'] for e in events), latest_error_id=events[0]['error_id'] if events else None,
                note=value.get('note'), automatic_upload=False)


def clear(root=None):
    # Delete known diagnostic files only; never touch business receipts or sessions.
    root = Path(root) if root is not None else directory()
    if root.is_symlink():
        raise ValueError('诊断目录校验失败。')
    try:
        if root.exists():
            with _storage_lock(root):
                # A corrupt/full log can also be reset. Exact filenames only and
                # writers share this lock; never recursively remove a directory.
                paths = [root/n for n in ('events.sqlite3', 'events.sqlite3-journal', 'events.sqlite3-wal', 'events.sqlite3-shm')]
                if any(p.is_symlink() for p in paths):
                    raise ValueError('diagnostic path redirected')
                for path in paths:
                    path.unlink(missing_ok=True)
        return {'cleared': True, 'business_records_preserved': True}
    except Exception:
        raise ValueError('日志清理未完成，请检查磁盘和目录权限；业务记录保留。') from None


def export(root=None):
    value = report(root)
    if not value['available']:
        raise ValueError(value['note'])
    return json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')


def install(mcp, service):
    """Observe real stdio calls as well as internal dispatch; preserve exceptions."""
    if getattr(mcp, '_safety_log_installed', False):
        return
    original = mcp.call_tool
    async def observed(name, arguments):
        started = time.monotonic()
        try:
            raw = await original(name, arguments)
        except Exception as exc:
            try:
                from .recovery import failure
                from .agent.catalog import WORKFLOWS
                modes = {flow['tools'][name] for flow in WORKFLOWS.values() if name in flow['tools']}
                outcome = 'read_failed' if modes == {'read'} else 'unknown'
                if modes == {'prepare'} and service in {'drive', 'spaces', 'vlab'}:
                    outcome = 'local_failed'
                problem = attach(failure(exc, write_may_have_run=outcome == 'unknown'), service=service,
                                 tool=name, exc=exc, outcome=outcome, duration_ms=int((time.monotonic()-started)*1000))
                exc._school_diagnostic = {k: problem[k] for k in ('error_id', 'diagnostic_saved', 'diagnostic_note') if k in problem}
            except Exception:
                pass
            raise
        # Normal typed failures also need an incident. Keep textual and structured
        # MCP content consistent, and never rewrite arbitrary resource results.
        try:
            from .recovery import returned_failure
            if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], dict):
                problem = returned_failure(raw[1])
                if problem and not raw[1].get('error_id'):
                    from .agent.catalog import WORKFLOWS
                    from mcp.types import TextContent
                    modes = {flow['tools'][name] for flow in WORKFLOWS.values() if name in flow['tools']}
                    incident = attach(problem, service=service, tool=name,
                                      outcome='read_failed' if modes == {'read'} else 'unknown',
                                      duration_ms=int((time.monotonic()-started)*1000))
                    if len(raw[0]) == 1 and isinstance(raw[0][0], TextContent):
                        value = {**raw[1], **{k: incident[k] for k in ('error_id', 'diagnostic_saved', 'diagnostic_note') if k in incident}}
                        raw = ([TextContent(type='text', text=json.dumps(value, ensure_ascii=False))], value)
        except Exception:
            pass
        return raw
    mcp.call_tool = observed
    mcp._safety_log_installed = True


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Export sanitized local diagnostics; never uploads data')
    parser.add_argument('--directory', help='Diagnostic directory only; does not read other files')
    parser.add_argument('--output', required=True, help='New JSON file, never overwritten')
    args = parser.parse_args()
    with Path(args.output).open('xb') as handle:
        handle.write(export(args.directory))
