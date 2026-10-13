"""Fixed business adapters on private framed pipes. No HTTP or chat runtime."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import time

MAX_FRAME = 16 * 1024 * 1024
CHAT_ACTIONS = {'task_context', 'tool_dispatch', 'connect', 'diagnostic'}
ACTIONS = CHAT_ACTIONS | {
    'metadata', 'service_check', 'mail_bind', 'campus_sites', 'campus_profile',
    'campus_profile_save', 'campus_timetable', 'campus_timetable_refresh',
    'digest_overview', 'digest_plan', 'digest_preview_report', 'digest_configure',
    'digest_refresh', 'digest_read', 'digest_courses', 'digest_calendar',
    'digest_summarize', 'digest_resume', 'diagnostics_report', 'diagnostics_export',
    'diagnostics_clear', 'diagnostics_record', 'school_model_keys',
    'school_model_import', 'background_status', 'tasks_list',
}
READ_ACTIONS = {'metadata', 'diagnostics_report', 'diagnostics_export', 'diagnostics_record',
                'diagnostic', 'background_status', 'campus_sites'}


@contextlib.contextmanager
def business_lock(root):
    path = Path(root) / 'adapter-business.lock'
    if path.is_symlink():
        raise OSError('redirected lock')
    with path.open('a+b') as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        obtained = False
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            obtained = True
        except OSError:
            pass
        try:
            yield obtained
        finally:
            if obtained:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def adapter_running(root):
    with business_lock(root) as available:
        return not available


class Stop:
    def __init__(self, path):
        self.path = path

    def is_set(self):
        return self.path.exists()

    def wait(self, seconds):
        end = time.monotonic() + max(0, seconds)
        while not self.is_set() and time.monotonic() < end:
            time.sleep(min(.1, end - time.monotonic()) if end > time.monotonic() else 0)
        return self.is_set()


def dispatch(data, action, value, stopped, notify):
    if action not in ACTIONS or not isinstance(value, dict):
        raise ValueError('invalid adapter action')
    if action in CHAT_ACTIONS:
        from .chat_legacy import handle
        return handle(data.root, action, value, stopped, notify)
    if action == 'metadata':
        from ..mail.onboarding import local_status
        return {'mail': local_status()}
    if action == 'service_check':
        from .connections import connect
        # Restoration belongs to Rust login; never invoke Python reconnect here.
        return connect(value['service'], stopped, notify, force=False, restore=False)
    if action == 'mail_bind':
        from ..mail.browser_setup import run
        progress = lambda v: notify(v.get('state', 'binding'), v.get('next_step', '请完成邮箱登录。'))
        return run(resume=True, notify=progress) if value.get('resume') is True else run(
            headed=True, create_client_password=True, notify=progress)
    if action == 'tasks_list':
        from ..agent.server import school_agent_tasks
        return school_agent_tasks(offset=value.get('offset', 0), limit=20)
    if action == 'background_status':
        from ..digest.store import Store
        from ..digest.worker import running
        from ..login_runtime import SERVICES, worker_pid, worker_running
        return {'digest_enabled': Store().read()['settings']['enabled'],
                'digest_running': running(), 'login_workers_running': any(
                    (pid := worker_pid(s)) and worker_running(s, pid) for s in SERVICES),
                'adapter_running': adapter_running(data.root),
                'adapter_uncertain': (data.root / 'adapter-uncertain.dpapi').exists()}
    if action.startswith('campus_'):
        from . import campus
        return {
            'campus_sites': lambda: campus.sites(),
            'campus_profile': lambda: campus.profile(data),
            'campus_profile_save': lambda: campus.save_profile(data, value.get('values'), value.get('revision')),
            'campus_timetable': lambda: campus.timetable(data),
            'campus_timetable_refresh': lambda: campus.refresh_timetable(data, value.get('semester_id', 0)),
        }[action]()
    if action.startswith('diagnostics_'):
        from .. import safety_log
        if action == 'diagnostics_record':
            return safety_log.record(**{k: value[k] for k in ('code', 'service', 'stage', 'tool', 'outcome') if k in value})
        if action == 'diagnostics_export':
            return json.loads(safety_log.export())
        return (safety_log.report if action == 'diagnostics_report' else safety_log.clear)()
    if action.startswith('school_model_'):
        from .school_model import keys, import_key
        return keys() if action == 'school_model_keys' else import_key(
            data, value.get('key_id'), value.get('model'), notify, stopped)
    from ..digest import engine
    if action == 'digest_overview':
        from ..digest.server import school_digest_overview
        return school_digest_overview()
    if action == 'digest_plan':
        from ..digest.planner import propose
        from .model import ModelClient
        from .storage import read_private
        request = value.get('request', '')
        model = ModelClient(read_private(data.settings_path)) if request else None
        return propose(request, value.get('preset'), engine.overview()['settings'], model)
    if action == 'digest_preview_report':
        return engine.preview_report()
    if action in {'digest_configure', 'digest_resume'}:
        from ..digest.worker import ensure
        if action == 'digest_resume':
            return ensure()  # This adapter is not a long-lived digest host.
        result = engine.configure(value.get('settings'), value.get('authorized') is True)
        if result['settings']['enabled']:
            result['worker'] = ensure()
        return result
    if action == 'digest_refresh':
        return engine.refresh()
    if action == 'digest_read':
        return engine.mark_read(value.get('message_id'))
    if action == 'digest_courses':
        from ..bb.client import BBClient
        return BBClient().courses()
    if action == 'digest_calendar':
        from ..digest.server import school_digest_export_calendar
        from .artifacts import register_local
        result = asyncio.run(school_digest_export_calendar(value.get('item_ids'), value.get('semester_id')))
        chat = data.new_chat()
        chat['title'] = '校园日历'
        register_local(data, chat, result['path'], 'calendar-exports', result['filename'])
        data.save_chat(chat)
        return {k: v for k, v in result.items() if k != 'path'} | {'chat_id': chat['id']}
    if action == 'digest_summarize':
        from .model import ModelClient
        from .storage import read_private
        from ..digest.summary import summarize
        return summarize(engine.overview(), ModelClient(read_private(data.settings_path)))
    raise ValueError('invalid adapter action')


def _read(stream, length):
    result = bytearray()
    while len(result) < length:
        block = stream.read(length - len(result))
        if not block:
            raise EOFError
        result.extend(block)
    return bytes(result)


def main():
    source, target = sys.stdin.buffer, sys.stdout.buffer
    def send(value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if not 0 < len(raw) <= MAX_FRAME:
            raise ValueError('adapter capacity')
        target.write(struct.pack('>I', len(raw)) + raw)
        target.flush()
    bound_root, data = None, None
    while True:
        try:
            size = struct.unpack('>I', _read(source, 4))[0]
            if not 0 < size <= MAX_FRAME:
                return
            def unique(items):
                value = {}
                for key, item in items:
                    if key in value:
                        raise ValueError('duplicate key')
                    value[key] = item
                return value
            request = json.loads(_read(source, size), object_pairs_hook=unique,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (EOFError, ValueError):
            return
        try:
            if not isinstance(request, dict) or request.get('action') not in ACTIONS:
                raise ValueError('invalid action')
            root = Path(request['root'])
            stop_name = request['stop_name']
            if not root.is_absolute() or not re.fullmatch(r'\.adapter-stop-[a-f0-9]{32}', stop_name):
                raise ValueError('invalid private scope')
            if bound_root is not None and root != bound_root:
                raise ValueError('adapter account scope changed')
            with open(os.devnull, 'w', encoding='utf-8') as sink, contextlib.redirect_stdout(sink):
                if data is None:
                    from .storage import Data
                    data, bound_root = Data(root), root
                with (contextlib.nullcontext(True) if request['action'] in READ_ACTIONS else business_lock(root)) as available:
                    if not available:
                        from ..service_errors import ServiceError
                        raise ServiceError('另一项兼容业务仍在结束，请等待原回执。', code='login_busy')
                    result = dispatch(data, request['action'], request.get('input'), Stop(root / stop_name),
                                      lambda stage, detail: send({'event': {'stage': stage, 'detail': detail}}))
            send({'ok': True, 'result': result})
        except Exception as exc:
            from ..recovery import REASONS
            from .. import safety_log
            code = getattr(exc, 'code', 'invalid_arguments' if isinstance(exc, ValueError) else 'runtime_failure')
            if code not in set(REASONS) | safety_log.MODEL_CODES | {'runtime_failure', 'task_access_denied'}:
                code = 'runtime_failure'
            incident = safety_log.record(code=code, stage='api', exc=exc, outcome='unknown')
            # Exception prose can contain credentials, paths or school responses.
            send({'ok': False, 'error': {'code': code, 'message': '本机兼容业务未完成，请查看诊断并核实原记录。', **incident}})


if __name__ == '__main__':
    main()
