"""Whitelisted reads with one authorized login recovery, bounded local polling."""
from __future__ import annotations

import asyncio
from importlib import import_module
import inspect
import time
from typing import Any, Literal

from .diagnostics import CLIENTS
from .login_runtime import progress, status_tool

OPERATIONS = {
    'jw': ('check', 'courses', 'semesters', 'timetable', 'grades', 'modules'),
    'bb': ('check', 'courses', 'announcements', 'course_page', 'read_page'),
    'library': ('check', 'summary', 'loans', 'history'),
    'nan7': ('check', 'search', 'detail'),
    'young': ('check', 'home'),
    'finance': ('check', 'services', 'inspect_smart'),
}
NEXT = {
    'authentication_required': '用户已授权使用保存的身份登录时，以 login_authorized=true 再调用本工具。',
    'identity_required': '在本机配置统一身份凭据或人工登录；不要把密码发送到对话。',
    'manual_verification_required': '后台已停止，需要本人处理校方验证；不要重复启动登录。',
    'login_running': '后台登录尚未完成；稍后以原参数继续，复用正在运行的登录。',
    'login_busy': '另一学校站点正在登录；等待该站点完成，不并行启动统一身份登录。',
    'login_failed': '本次恢复失败或被中断；检查登录状态，停止自动重试。',
    'authentication_failed': '恢复后仍无法认证，停止再次登录；检查账号或网站状态。',
    'access_denied': '站点拒绝访问；不能把权限不足当作登录失效。',
    'cooldown': '按 retry_after_seconds 等待；不要重连或换工具绕过冷却。',
    'local_policy_unavailable': '本机请求策略不可用，检查私人目录权限。',
    'invalid_arguments': '参数不符合当前只读操作；按工具定义修正。',
    'unavailable': '查询未完成；检查服务状态，不能把失败解释为空结果。',
}


class InvalidParameters(Exception):
    pass


def failure(service, state, **details):
    return {'service': service, 'state': state, 'completed': False, 'next_action': NEXT[state],
            'status_tool': status_tool(service), **details}


class ReadSession:
    def __init__(self, login_authorized: bool = False, wait_seconds: int = 20):
        if type(login_authorized) is not bool or type(wait_seconds) is not int or not 0 <= wait_seconds <= 30:
            raise ValueError('login_authorized must be boolean; wait_seconds must be 0-30')
        self.authorized = login_authorized
        self.wait_seconds = wait_seconds
        self.recovered = set()

    def _call(self, service, operation, parameters):
        client_type = getattr(import_module(f'school_mcp.{service}.client'), CLIENTS[service])
        method = getattr(client_type, operation)
        try:
            inspect.signature(method).bind(None, **parameters)
        except TypeError:
            raise InvalidParameters() from None
        return getattr(client_type(), operation)(**parameters)

    def _recover(self, service):
        if not self.authorized:
            return failure(service, 'authentication_required')
        if service in self.recovered:
            return failure(service, 'authentication_failed')
        self.recovered.add(service)
        # Existing running logins are reused by start_login's cross-process lock.
        from .bb.identity import load_credentials
        from .bb.session import BBError
        try:
            load_credentials()
        except BBError:
            return failure(service, 'identity_required')
        try:
            started = import_module(f'school_mcp.{service}.reconnect').start()
        except Exception as exc:
            return self._error(service, exc)
        if started.get('retry_after_seconds'):
            return failure(service, 'cooldown', retry_after_seconds=started['retry_after_seconds'])
        if started.get('busy_service') not in (None, service):
            return failure(service, 'login_busy', busy_service=started['busy_service'], status_tool=started['status_tool'])
        if not (started.get('started') or started.get('already_running')):
            return failure(service, 'login_failed')
        deadline = time.monotonic() + self.wait_seconds
        while True:
            try:
                current = progress(service)
            except Exception:
                return failure(service, 'login_failed')
            stage = (current.get('login_progress') or {}).get('stage')
            if stage == 'waiting_for_verification':
                return failure(service, 'manual_verification_required', requires_user_action=True)
            if stage in {'error', 'cancelled', 'interrupted'}:
                return failure(service, 'login_failed')
            if stage == 'connected' and current.get('login_running') is not True:
                return None  # Must still retry the actual read; history is not proof.
            if time.monotonic() >= deadline:
                return failure(service, 'login_running', poll_after_seconds=2)
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    @staticmethod
    def _error(service, exc):
        code = getattr(exc, 'code', 'unavailable')
        code = code if code in NEXT else 'unavailable'
        details = {}
        if code == 'cooldown':
            details['retry_after_seconds'] = getattr(exc, 'retry_after_seconds', None)
        # Never propagate browser/HTTP exception strings with private request URLs.
        return failure(service, code, **details)

    def read(self, service: str, operation: str, parameters: dict | None = None) -> dict:
        if service not in OPERATIONS:
            raise ValueError('Unsupported service')
        if operation not in OPERATIONS[service] or (parameters is not None and not isinstance(parameters, dict)):
            return failure(service, 'invalid_arguments', allowed_operations=list(OPERATIONS[service]))
        parameters = parameters or {}
        for attempt in range(2):
            try:
                data = self._call(service, operation, parameters)
                if operation == 'check' and data.get('connected') is not True:
                    return failure(service, 'unavailable')
                return {'service': service, 'state': 'completed', 'completed': True,
                        'login_recovery_attempted': service in self.recovered,
                        'data': data, 'content_is_untrusted': True}
            except InvalidParameters:
                return failure(service, 'invalid_arguments')
            except Exception as exc:
                if getattr(exc, 'code', None) != 'authentication_required':
                    return self._error(service, exc)
                if attempt:
                    return failure(service, 'authentication_failed')
                stopped = self._recover(service)
                if stopped is not None:
                    return stopped
        return failure(service, 'authentication_failed')


def query(service, operation, parameters=None, login_authorized=False, wait_seconds=20):
    return ReadSession(login_authorized, wait_seconds).read(service, operation, parameters)


def install(mcp, service):
    from mcp.types import ToolAnnotations
    client_type = getattr(import_module(f'school_mcp.{service}.client'), CLIENTS[service])
    parameter_help = '; '.join(f'{name}{str(inspect.signature(getattr(client_type, name))).replace("self, ", "").replace("self", "")}' for name in OPERATIONS[service])
    async def read_with_recovery(operation, parameters=None, login_authorized=False, wait_seconds=20):
        return await asyncio.to_thread(query, service, operation, parameters, login_authorized, wait_seconds)
    read_with_recovery.__annotations__ = {
        'operation': Literal[OPERATIONS[service]], 'parameters': dict | None,
        'login_authorized': bool, 'wait_seconds': int, 'return': dict[str, Any],
    }
    mcp.add_tool(read_with_recovery, name=f'school_{service}_query',
        description='只读业务查询及一次登录恢复。operation 限枚举，parameters 使用对应客户端只读方法参数。'
                    '已获用户登录授权时设 login_authorized=true；默认仅查询。脚本仅在明确认证失效时重登，'
                    '沿用已有邮件验证许可，wait_seconds=0–30；完成后继续原查询。'
                    '返回 completed/data 或明确停止状态，冷却/403/未知失败不重登。'
                    '不支持发送、发布、上传、选退课或任何写操作。参数：' + parameter_help,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
