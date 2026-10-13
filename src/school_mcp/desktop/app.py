from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid

from .storage import Data, now, read_private, write_private
from .model import ModelError, public_settings, save_settings, test_connection, list_models, PROVIDERS
from .chat import ChatRunner, ui_view
from ..agent.catalog import adapter
from ..agent.server import school_agent_tasks
from ..bb.identity import load_credentials, set_email_verification
from ..bb.session import BBError
from ..mail.onboarding import local_status as mail_status
from ..login_runtime import progress, SERVICES
from ..recovery import failure, returned_failure
from .. import safety_log

LABELS = {'mail': '学校邮箱', 'bb': 'Blackboard', 'jw': '教务系统', 'young': '青春科大',
          'finance': '财务平台', 'library': '图书馆', 'teach': '教务处通知', 'icourse': '评课社区', 'nan7': '南七集市',
          'portal': '一网通办', 'student': '学工与奖学金', 'spaces': '学习空间', 'vlab': 'Vlab 虚拟机', 'drive':'科大云盘'}
SESSION_FILES = {'bb': 'bb.session.dpapi', 'jw': 'jw.session.dpapi', 'library': 'library.session.dpapi',
                 'young': 'young-session.dpapi', 'finance': 'finance-session.dpapi',
                 'nan7': 'nan7-session.dpapi', 'icourse': 'icourse-session.dpapi',
                 'portal': 'portal-campus-session.dpapi', 'student': 'student-campus-session.dpapi',
                 'spaces': 'spaces-campus-session.dpapi', 'vlab': 'vlab-campus-session.dpapi','drive':'drive-campus-session.dpapi'}


class Application:
    def __init__(self, root=None):
        self.data = Data(root)
        self.mutex = threading.RLock()
        self.stop_event = threading.Event()
        self.worker = None
        self.closing = False
        self.operation = read_private(self.data.root / 'operation.dpapi', {'state': 'idle'})
        if self.operation.get('state') == 'running':
            self.operation = {'state': 'interrupted', 'detail': '上次操作中断，请先查看原记录。',
                              **safety_log.record(code='interrupted', stage='recovery', outcome='interrupted')}
        self.connections = {}
        self.chat_id = None

    def identity(self):
        try:
            value = load_credentials()
            return {'saved': True, 'account': value['username'], 'session_only': value.get('session_only', False),
                    'email_verification': value.get('email_verification_enabled', False)}
        except BBError:
            return {'saved': False, 'account': None, 'email_verification': False}

    def require_login(self):
        if not self.identity()['saved'] or (self.data.root / 'locked').exists():
            raise ValueError('请先登录学校统一身份。')

    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def accepting(self):
        if self.closing:
            raise ValueError('应用正在退出，请重新打开后操作。')

    def shutdown(self):
        with self.mutex:
            if self.busy():
                raise ValueError('请先停止或等待当前操作完成。')
            self.closing = True
            return {'closed': True}

    def status(self):
        chrome = any(p.exists() for p in (Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe',
                    Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'Google/Chrome/Application/chrome.exe'))
        identity = self.identity()
        locked = (self.data.root / 'locked').exists()
        if locked:
            identity = {**identity, 'account': None}
        operation = self.operation if not locked else {'state': 'idle'}
        return dict(identity=identity, locked=(self.data.root / 'locked').exists(), mail=mail_status(),
                    model=public_settings(self.data), model_providers=PROVIDERS, chrome_installed=chrome, busy=self.busy(),
                    operation=operation, services=[dict(service=k, label=v,
                        session_saved=not locked and ((self.data.profile / SESSION_FILES[k]).exists() if k in SESSION_FILES else k == 'mail' and mail_status().get('configured', False)),
                        **self.connections.get(k, {'state': 'unchecked'})) for k,v in LABELS.items()],
                    data_location=str(self.data.root), version='0.1.0', active_chat=self.chat_id)

    def notify(self, stage, detail):
        with self.mutex:
            self.operation.update(stage=stage, detail=detail)
            write_private(self.data.root / 'operation.dpapi', self.operation)

    def start(self, kind, function):
        with self.mutex:
            self.accepting()
            if self.busy():
                raise ValueError('当前有操作进行中，请先等待完成或停止。')
            self.stop_event.clear()
            self.operation = dict(id=uuid.uuid4().hex, kind=kind, state='running', stage='starting',
                                  detail='正在处理…', started_at=now())
            write_private(self.data.root / 'operation.dpapi', self.operation)
            def work():
                started = time.monotonic()
                stage = 'login' if kind.startswith('login_') else 'connection' if kind.startswith('connect_') else 'model' if 'model' in kind else 'background'
                try:
                    result = function() or {}
                    problem = returned_failure(result)
                    if problem:
                        result = safety_log.attach(problem, stage=stage, service=result.get('service', 'app'),
                                                   duration_ms=int((time.monotonic()-started)*1000)) | result
                    attention = isinstance(result, dict) and (result.get('operation_failed') or result.get('requires_user_action')
                        or result.get('state') in {'creation_attempted', 'manual_required', 'captured'})
                    with self.mutex:
                        state = 'attention' if attention else 'completed'
                        if isinstance(result, dict) and result.get('state') in {'interrupted', 'stopped', 'waiting_for_approval'}:
                            state = result['state']
                        self.operation.update(state=state, result=result,
                            detail=(result.get('next_step') or result.get('note') or '本次处理已结束，请查看结果。') if isinstance(result, dict) else '本次处理已结束。')
                        if isinstance(result, dict):
                            self.operation.update({k: result[k] for k in ('error_id', 'diagnostic_saved', 'diagnostic_note') if k in result})
                except ValueError as exc:
                    # Application ValueErrors are fixed messages. ValidationErrors handled at API ingress.
                    from pydantic import ValidationError
                    detail = '输入格式不正确，请检查设置。' if isinstance(exc, ValidationError) else str(exc)
                    with self.mutex:
                        self.operation.update(state='failed', detail=detail[:500])
                        if isinstance(exc, ModelError):
                            self.operation['error_code'] = exc.code
                        incident = getattr(exc, 'diagnostic', None) or safety_log.record(
                            code=getattr(exc, 'code', 'invalid_arguments'), stage=stage, exc=exc,
                            outcome='not_started' if stage == 'model' else 'unknown',
                            duration_ms=int((time.monotonic()-started)*1000))
                        self.operation.update({k: incident[k] for k in ('error_id', 'diagnostic_saved', 'diagnostic_note') if k in incident})
                except Exception as exc:
                    with self.mutex:
                        self.operation.update(state='failed', detail='操作未完成，请检查网络、浏览器或站点状态；已有记录保留。')
                        self.operation.update(safety_log.record(stage=stage, exc=exc, outcome='unknown',
                                              duration_ms=int((time.monotonic()-started)*1000)))
                finally:
                    try:
                        write_private(self.data.root / 'operation.dpapi', self.operation)
                    except Exception as exc:
                        self.operation.update(state='failed', detail='本机进度保存失败；先核实原任务，不重复执行。',
                                              **safety_log.record(code='storage_failed', stage='persistence', exc=exc, outcome='unknown'))
            self.worker = threading.Thread(target=work, daemon=True)
            self.worker.start()
            return {'operation_id': self.operation['id'], 'started': True}

    def login(self, service):
        if service == 'identity':
            from .auth import manual_identity
            def run():
                result = manual_identity(self.notify)
                (self.data.root / 'locked').unlink(missing_ok=True)
                self.connections['bb'] = {'state': 'connected', 'checked_at': now()}
                return result
        elif service == 'mail':
            self.require_login()
            from ..mail.browser_setup import run as bind
            def run():
                if mail_status().get('configured'):
                    result = self.ensure_service('mail', restore=False)
                    if result.get('connected') is True:
                        return {'connected': True, 'session_reused': True, 'note': '已有邮箱配置有效，无需重复创建客户端密码。'}
                    return {**result, 'requires_user_action': True}
                result = bind(headed=True, create_client_password=True, notify=lambda value: self.notify(value.get('state', 'binding'), value.get('next_step', '请在邮箱官方窗口完成登录。')))
                return result
        elif service == 'mail_resume':
            self.require_login()
            from ..mail.browser_setup import run as bind
            run = lambda: bind(resume=True)
        elif service == 'icourse':
            self.require_login()
            from .auth import manual_icourse
            def run():
                result = manual_icourse(self.notify)
                self.connections['icourse'] = {'state': 'connected', 'checked_at': now()}
                return result
        elif service in {'portal','student','spaces','drive','vlab'}:
            self.require_login()
            from ..campus.login import manual
            def run():
                result=self.ensure_service(service,restore=False)
                if result.get('connected') is True:return result
                if result.get('error_code') not in {'authentication_required','identity_required','manual_verification_required'}:
                    return {**result,'requires_user_action':True}
                result=manual(service,self.notify,self.stop_event)
                self.connections[service]={'state':'connected','checked_at':now()}
                return result
        elif service in SERVICES:
            self.require_login()
            from .auth import manual_service
            def run():
                # A manual login button still reuses a currently valid session.
                result = self.ensure_service(service, restore=False)
                if result.get('connected') is True:
                    return {'connected': True, 'session_reused': True, 'note': '已有会话有效，无需再次登录。'}
                if result.get('error_code') not in {'authentication_required', 'identity_required', 'manual_verification_required'}:
                    return {**result, 'requires_user_action': True}
                result = manual_service(service, self.notify, self.stop_event)
                self.connections[service] = {'state': 'connected', 'checked_at': now()}
                return result
        else:
            raise ValueError('不支持的登录入口。')
        return self.start('login_' + service, run)

    def ensure_service(self, service, force=False, restore=True):
        from .connections import connect
        self.require_login()
        if service not in LABELS:
            raise ValueError('未知学校平台。')
        value = connect(service, self.stop_event, self.notify, force=force, restore=restore)
        problem = returned_failure(value, service=service)
        if problem:
            problem = safety_log.attach(problem, service=service, stage='connection', outcome='read_failed')
            code = problem['error_code']
            state = ('login_required' if code in {'authentication_required', 'identity_required'} else
                     'verification_required' if code == 'manual_verification_required' else 'unavailable')
            self.connections[service] = dict(state=state, checked_at=now(), error_code=code,
                                            next_step=problem['next_step'], recovery=problem['recovery'])
            return problem
        self.connections[service] = {'state': 'connected' if value.get('connected') is True else 'unavailable', 'checked_at': now()}
        return value

    def check_service(self, service, reconnect=False):
        self.require_login()
        if service not in LABELS:
            raise ValueError('未知学校平台。')
        return self.start('connect_' + service, lambda: self.ensure_service(service, reconnect, restore=reconnect))

    def send(self, value):
        self.accepting()
        self.require_login()
        if self.busy():
            raise ValueError('当前有操作进行中，请先等待完成或停止。')
        if not isinstance(value.get('text'), str) or not 1 <= len(value['text'].strip()) <= 12000:
            raise ValueError('请输入 1–12000 字的需求。')
        if type(value.get('execute_authorized', False)) is not bool:
            raise ValueError('办理许可格式无效。')
        files = ChatRunner.validate_files(self.data, value.get('files', []))
        if not public_settings(self.data)['tested']:
            raise ValueError('请先在模型设置中保存并测试 API 连接。')
        identifier = value.get('chat_id')
        if identifier:
            chat = self.data.chat(identifier)
            if chat['pending']:
                raise ValueError('请先确认或取消当前待执行操作。')
        else:
            identifier = self.data.new_chat()['id']
        self.chat_id = identifier
        result = self.start('chat', lambda: ChatRunner(self.data, self.stop_event, connect=self.ensure_service).run(identifier, value['text'],
                           value.get('execute_authorized', False), files))
        return {**result, 'chat_id': identifier}

    def approval(self, identifier, approval_id, approve):
        self.require_login()
        if self.busy():
            raise ValueError('请先等待当前操作完成。')
        if type(approve) is not bool:
            raise ValueError('确认值无效。')
        chat = self.data.chat(identifier)
        if not chat['pending'] or chat['pending']['id'] != approval_id:
            raise ValueError('操作已变化，请刷新。')
        if not approve:
            chat['turn_authorized'] = False
            chat['messages'].append({'role': 'tool', 'tool_call_id': chat['pending']['call_id'],
                'content': '{"state":"cancelled","note":"用户取消了这项操作，不要自动重建或提交。"}'})
            chat['pending'] = None
            chat['state'] = 'idle'
            self.data.save_chat(chat)
            return {'cancelled': True}
        return self.start('chat', lambda: ChatRunner(self.data, self.stop_event, connect=self.ensure_service).run(identifier, approval=approval_id))

    def upload(self, name, content):
        self.accepting()
        self.require_login()
        from . import native_core
        if native_core.enabled():
            from .app_native import upload
            return upload(self.data, name, content)
        if not name or len(name) > 180 or Path(name).name != name or any(c in name for c in '<>:"/\\|?*\x00'):
            raise ValueError('附件名称无效。')
        if not 0 < len(content) <= 50 * 1024 * 1024:
            raise ValueError('单个附件须在 50 MiB 以内。')
        directory = self.data.profile / 'uploads' / uuid.uuid4().hex
        directory.mkdir(parents=True)
        path = directory / name
        path.write_bytes(content)
        return {'name': name, 'path': str(path), 'size': len(content)}

    def background_active(self):
        from ..login_runtime import worker_pid, worker_running
        if any((pid := worker_pid(s)) and worker_running(s, pid) for s in SERVICES):
            return True
        from ..script_jobs import JobStore, TERMINAL
        database = self.data.profile / 'script-jobs.sqlite3'
        if database.exists():
            import sqlite3
            from ..mail.credentials import _dpapi
            with sqlite3.connect(database) as db:
                rows = db.execute('SELECT payload FROM jobs').fetchall()
            jobs = [json.loads(_dpapi(r[0], decrypt=True)) for r in rows]
            if any(j.get('state') not in TERMINAL or j.get('effect_started') for j in jobs):
                return True
        schedules = (self.data.profile / 'young-schedules').glob('*.json')
        # Unknown schedule state blocks account changes; never delete a potentially armed job.
        for path in schedules:
            value = json.loads(path.read_text(encoding='utf-8'))
            if not value.get('cancelled') and value.get('worker_state') not in {'completed', 'cancelled', 'failed', 'expired', 'verified', 'stopped'}:
                return True
        return False

    def clear(self, scope, confirmation):
        from ..digest.worker import running as digest_running
        from ..digest.store import Store as DigestStore
        if digest_running() or DigestStore().read()['settings']['enabled']:
            raise ValueError('请先在信息中心暂停推送，等待当前采集或发送结束，再管理账号。')
        if self.busy() or self.background_active():
            raise ValueError('还有运行中或待核实的后台任务，请先在任务记录中停止并核实。')
        if scope not in {'logout', 'mail', 'account', 'all'} or confirmation != scope:
            raise ValueError('请明确确认需要清理的范围。')
        if scope == 'logout':
            (self.data.root / 'locked').write_text('locked', encoding='utf-8')
            return {'locked': True, 'note': '已锁定应用。重新登录后才能访问原账号记录。'}
        if scope == 'mail':
            self.require_login()
            journal = self.data.profile / 'mail-browser-setup.dpapi'
            binding = read_private(journal, {})
            if binding.get('state') == 'captured':
                # Remove a pending secret while retaining the once-only creation marker.
                write_private(journal, {'state': 'creation_attempted', 'label': binding.get('label'), 'updated_at': now()})
            for name in ('mail.json', 'mail.credentials.dpapi'):
                (self.data.profile / name).unlink(missing_ok=True)
            try:
                set_email_verification(False)
            except BBError:
                pass
            # Keep the once-only browser binding journal to avoid minting another key accidentally.
            return {'unbound': True, 'note': '已移除本机邮箱凭据并关闭验证码回退；网站客户端密码需在邮箱安全设置中撤销。'}
        target = self.data.profile.resolve()
        if target != self.data.root / 'profile' or target.is_symlink() or not target.is_relative_to(self.data.root):
            raise ValueError('数据目录校验失败。')
        shutil.rmtree(target)
        target.mkdir()
        (self.data.root / 'locked').unlink(missing_ok=True)
        if scope == 'all':
            self.data.settings_path.unlink(missing_ok=True)
            safety_log.clear()
        self.connections.clear()
        self.chat_id = None
        self.operation = {'state': 'idle'}
        write_private(self.data.root / 'operation.dpapi', self.operation)
        return {'cleared': scope, 'model_settings_preserved': scope == 'account'}

    def api(self, path, value):
        self.accepting()
        if path.startswith('/api/campus/'):
            self.require_login()
            from . import campus
            if path == '/api/campus/profile':
                if self.busy():
                    raise ValueError('请等待当前操作完成后保存资料。')
                return campus.save_profile(self.data, value.get('values'), value.get('revision'))
            if path == '/api/campus/timetable/refresh':
                return self.start('campus_timetable', lambda: campus.refresh_timetable(self.data, value.get('semester_id', 0)))
            raise ValueError('未知校园页面操作。')
        if path.startswith('/api/digest/'):
            self.require_login()
            from ..digest import engine
            if path == '/api/digest/plan':
                def plan():
                    from ..digest.planner import propose
                    from .model import ModelClient
                    request=value.get('request','');preset=value.get('preset')
                    model=ModelClient(read_private(self.data.settings_path)) if request else None
                    return propose(request,preset,engine.overview()['settings'],model)
                return self.start('digest_plan',plan)
            if path == '/api/digest/preview-report':
                return self.start('digest_preview_report',engine.preview_report)
            if path == '/api/digest/configure':
                if self.busy():
                    raise ValueError('请等待当前操作完成。')
                result = engine.configure(value.get('settings'), value.get('authorized') is True)
                if result['settings']['enabled']:
                    from ..digest.worker import ensure
                    result['worker'] = ensure(allow_in_process=True)
                return result
            if path == '/api/digest/refresh':
                return self.start('digest_refresh', engine.refresh)
            if path == '/api/digest/read':
                return engine.mark_read(value.get('message_id'))
            if path == '/api/digest/courses':
                from ..bb.client import BBClient
                return self.start('digest_courses', lambda: BBClient().courses())
            if path == '/api/digest/calendar':
                from ..digest.server import school_digest_export_calendar
                def export_calendar():
                    result = asyncio.run(school_digest_export_calendar(value.get('item_ids'), value.get('semester_id')))
                    chat = self.data.new_chat()
                    chat['title'] = '校园日历'
                    from .artifacts import register_local
                    register_local(self.data, chat, result['path'], 'calendar-exports', result['filename'])
                    self.data.save_chat(chat)
                    return {k:v for k,v in result.items() if k!='path'} | {'chat_id': chat['id']}
                return self.start('digest_calendar', export_calendar)
            if path == '/api/digest/summarize':
                def summarize():
                    from .model import ModelClient
                    from ..digest.summary import summarize as make_summary
                    model = ModelClient(read_private(self.data.settings_path))
                    return make_summary(engine.overview(), model)
                return self.start('digest_summary', summarize)
            raise ValueError('未知信息中心操作。')
        if path == '/api/diagnostics/clear':
            if value.get('confirmation') != 'diagnostics':
                raise ValueError('请确认仅清理诊断日志。')
            return safety_log.clear()
        if path == '/api/diagnostics/ui-error':
            # Never accept JS message, stack, URL or input values from the browser.
            return safety_log.record(code='ui_error', stage='ui', outcome='unknown')
        if path == '/api/login':
            return self.login(value.get('service'))
        if path == '/api/model/save':
            if self.busy():
                raise ValueError('请先等待当前操作完成。')
            return save_settings(self.data, value)
        if path == '/api/model/test':
            return self.start('model_test', lambda: test_connection(self.data, self.notify, self.stop_event))
        if path == '/api/model/connect':
            if self.busy():
                raise ValueError('请先等待当前操作完成。')
            save_settings(self.data, value)
            return self.start('model_test', lambda: test_connection(self.data, self.notify, self.stop_event))
        if path == '/api/model/models':
            if self.busy():
                raise ValueError('请先等待当前操作完成。')
            return self.start('model_list', lambda: list_models(self.data, value))
        if path == '/api/model/school-keys':
            self.require_login()
            from .school_model import keys
            return self.start('school_model_keys', keys)
        if path == '/api/model/school-connect':
            self.require_login()
            from .school_model import import_key
            return self.start('school_model_import', lambda: import_key(self.data, value.get('key_id'), value.get('model'), self.notify, self.stop_event))
        if path == '/api/connect':
            return self.check_service(value.get('service'), value.get('reconnect', False) is True)
        if path == '/api/email-verification':
            self.require_login()
            if self.busy():
                raise ValueError('请先等待当前操作完成。')
            if type(value.get('enabled')) is not bool:
                raise ValueError('设置值无效。')
            set_email_verification(value['enabled'])
            return {'enabled': value['enabled']}
        if path == '/api/chat/send':
            return self.send(value)
        if path == '/api/chat/approve':
            return self.approval(value['chat_id'], value['approval_id'], value['approve'])
        if path == '/api/stop':
            self.stop_event.set()
            return {'stop_requested': True, 'note': '停止后续步骤，已发出请求仍需核实。'}
        if path == '/api/clear':
            return self.clear(value.get('scope'), value.get('confirmation'))
        raise ValueError('未知操作。')
