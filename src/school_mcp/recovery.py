"""Fixed recovery contracts shared by the coordinator and desktop; no exception prose."""
import math
import re

import httpx
from pydantic import ValidationError
from playwright.sync_api import TimeoutError as BrowserTimeout, Error as BrowserError

from .service_errors import ServiceError
from .network import CooldownError, PolicyError


REASONS = {
    'authorization_required': ('confirm_operation', '操作尚未获准执行；核对具体预览和已有授权后继续。'),
    'authentication_required': ('restore_session', '登录已失效；在账号页恢复当前平台会话，仍失败时由本人重新验证。'),
    'identity_required': ('manual_login', '尚无可用身份；请在账号页由本人登录。'),
    'manual_verification_required': ('manual_verification', '自动恢复已停在学校验证页，尚未识别具体验证类型。打开 SchoolAgent 的“账号与连接 → 对应平台 → 打开验证窗口”，查看官方页面要求并完成验证；应用随后检查连接。'),
    'access_denied': ('check_permission', '平台拒绝访问；请核实账号权限，不要反复重新登录。'),
    'cooldown': ('wait_then_check', '请求正在冷却；按返回的等待时间检查连接，不要重连或换工具绕过冷却。'),
    'timeout': ('check_connection', '请求超时；检查网络和平台连接后再继续。'),
    'network_unavailable': ('check_connection', '网络连接未完成；检查网络和平台连接，不自动重新登录。'),
    'service_unavailable': ('check_connection', '平台暂时不可用；稍后检查连接，不自动重新登录。'),
    'local_policy_unavailable': ('check_local_storage', '本机请求记录不可用；检查私人目录权限后继续。'),
    'invalid_arguments': ('correct_arguments', '工具参数不符合要求；重新读取工具定义并修正参数。'),
    'task_access_denied': ('open_original_chat', '任务不属于当前对话；请打开原任务对应的对话。'),
    'file_not_authorized': ('select_file', '文件未经当前对话选择；请在输入框旁添加文件。'),
    'runtime_failure': ('check_runtime', '后台运行环境异常；检查本机 Chrome 和程序状态。'),
    'login_failed': ('check_login_status', '本次恢复失败或中断；检查登录进度，不自动重复启动登录。'),
    'login_busy': ('check_login_status', '已有登录流程进行中；等待原流程完成，不并行启动登录。'),
    'login_running': ('check_login_status', '后台登录尚未完成；稍后检查原登录进度。'),
    'outcome_unknown': ('verify_original_receipt', '结果尚未核实；查询原业务回执，不重复提交。'),
    'unavailable': ('inspect_task', '未取得可靠结果；检查平台及原任务状态，不把失败当作空结果。'),
}

from .login_guidance import MESSAGES
REASONS.update({code: ('manual_verification', message) for code, message in MESSAGES.items()})

# Compatibility mode retains the legacy fixed table until non-native MCPs migrate.
# Never let an exception attribute or website field become pipe text.
SERVICES = frozenset({'app', 'agent', 'model', 'mail', 'smtp', 'bb', 'jw', 'young',
    'finance', 'library', 'library-public', 'teach', 'icourse', 'nan7', 'portal',
    'student', 'spaces', 'rooms', 'hightech', 'drive', 'compute', 'vlab', 'web', 'digest', 'identity', 'llm'})
RETURN_STATES = frozenset(REASONS) | {'waiting_for_verification', 'authentication_failed',
    'needs_verification', 'uncertain', 'already_attempted'}
DIAGNOSTIC_UNAVAILABLE = '诊断日志无法写入，请检查磁盘空间和目录权限；本次错误编号仅供当前界面使用。'


def _choice(value, choices):
    return value if isinstance(value, str) and value in choices else None


def safe_retry_seconds(value):
    """Return a positive finite pipe-safe wait, or None for invalid metadata."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        if not math.isfinite(value) or value <= 0:
            return None
    except OverflowError:
        return None
    # serde_json's integer range is u64. Larger finite waits are represented as
    # floats, without shortening the cooldown to a bounded integer or a retry.
    if isinstance(value, int) and value > 2**64 - 1:
        seconds = float(value)
        if seconds < value:
            seconds = math.nextafter(seconds, math.inf)
        return seconds if math.isfinite(seconds) else None
    return value


def _diagnostic_fields(value):
    identifier = value.get('error_id')
    if (isinstance(identifier, str) and re.fullmatch('ERR-[A-F0-9]{16}', identifier)
            and type(value.get('diagnostic_saved')) is bool):
        result = {'error_id': identifier, 'diagnostic_saved': value['diagnostic_saved']}
        if value['diagnostic_saved'] is False and value.get('diagnostic_note') == DIAGNOSTIC_UNAVAILABLE:
            result['diagnostic_note'] = DIAGNOSTIC_UNAVAILABLE
        return result
    return {}


def _result_fields(value):
    """Only fixed codes and primitive recovery metadata may cross this pipe."""
    if not isinstance(value, dict):
        return None
    result = {'state': _choice(value.get('state'), RETURN_STATES),
              'retry_after_seconds': safe_retry_seconds(value.get('retry_after_seconds'))}
    if type(value.get('connected')) is bool:
        result['connected'] = value['connected']
    if value.get('operation_failed') is True and isinstance(value.get('recovery'), dict):
        result.update(operation_failed=True, recovery={},
                      error_code=_choice(value.get('error_code'), REASONS),
                      service=_choice(value.get('service'), SERVICES))
        if value['recovery'].get('action') == 'verify_original_receipt':
            result['recovery']['action'] = 'verify_original_receipt'
    result.update(_diagnostic_fields(value))
    return result


def classify(exc):
    """Only typed exceptions and explicit cause chains, never text matching."""
    seen = set()
    for _ in range(8):
        if exc is None or id(exc) in seen:
            break
        seen.add(id(exc))
        code = getattr(exc, 'code', None)
        if isinstance(exc, ServiceError) and _choice(code, REASONS) and code != 'unavailable':
            return code, getattr(exc, 'retry_after_seconds', None)
        if isinstance(exc, CooldownError):
            return 'cooldown', exc.retry_after_seconds
        if isinstance(exc, PolicyError):
            return 'local_policy_unavailable', None
        if isinstance(exc, (httpx.TimeoutException, TimeoutError, BrowserTimeout)):
            return 'timeout', None
        if isinstance(exc, httpx.HTTPStatusError):
            return {401: 'authentication_required', 403: 'access_denied', 429: 'cooldown',
                    408: 'timeout'}.get(exc.response.status_code, 'service_unavailable'), None
        if isinstance(exc, (httpx.TransportError, ConnectionError)):
            return 'network_unavailable', None
        if isinstance(exc, ValidationError):
            return 'invalid_arguments', None
        if isinstance(exc, BrowserError):
            return 'runtime_failure', None
        # FastMCP wraps handlers in ToolError, preserving __cause__.
        exc = exc.__cause__
    return 'unavailable', None


def failure(exc=None, *, code=None, service=None, write_may_have_run=False, retry_after_seconds=None):
    if code is None:
        code, retry_after_seconds = classify(exc)
    code = _choice(code, REASONS) or 'unavailable'
    service = _choice(service, SERVICES)
    retry_after_seconds = safe_retry_seconds(retry_after_seconds)
    from .desktop import native_core
    if native_core.enabled():
        return native_core.call('recovery_failure', {'code': code, 'service': service,
            'write_may_have_run': bool(write_may_have_run), 'retry_after_seconds': retry_after_seconds})
    action, note = REASONS[code]
    recovery = dict(action=action, automatic_retry=False, retry_write_allowed=False,
                    retry_read_after_check=code in {'timeout', 'network_unavailable', 'service_unavailable', 'cooldown'})
    if write_may_have_run:
        recovery.update(action='verify_original_receipt', after_verification=action, retry_read_after_check=False)
        note = '操作可能已产生效果；先查询原任务回执，不重复提交。' + note
    result = dict(state='needs_verification' if write_may_have_run else code, error_code=code,
                  operation_failed=True, completed=False, next_step=note, recovery=recovery)
    if service:
        result['service'] = service
    if retry_after_seconds is not None:
        result['retry_after_seconds'] = math.ceil(retry_after_seconds)
        result['next_step'] += f" 至少等待 {result['retry_after_seconds']} 秒。"
    return result


def returned_failure(value, *, service=None):
    """Normalize explicit failure states; never guess from notes or website prose."""
    value = _result_fields(value)
    service = _choice(service, SERVICES)
    from .desktop import native_core
    if native_core.enabled():
        return native_core.call('recovery_returned_failure', {'value': value, 'service': service})
    if not isinstance(value, dict):
        return failure(service=service)
    def preserve(problem):
        problem.update(_diagnostic_fields(value))
        return problem
    state = value.get('state')
    aliases = {'waiting_for_verification': 'manual_verification_required',
               'authentication_failed': 'authentication_required'}
    code = aliases.get(state, state) if isinstance(state, str) else None
    unknown_write = code in {'needs_verification', 'uncertain', 'already_attempted'}
    if value.get('operation_failed') is True and isinstance(value.get('recovery'), dict):
        code = value.get('error_code') or ('outcome_unknown' if unknown_write else code)
        write = unknown_write or (value['recovery'].get('action') == 'verify_original_receipt'
                                  and code != 'outcome_unknown')
        return preserve(failure(code=code or 'unavailable', service=service or value.get('service'),
            write_may_have_run=write, retry_after_seconds=value.get('retry_after_seconds')))
    if unknown_write:
        return preserve(failure(code='outcome_unknown', service=service, write_may_have_run=True))
    if code in REASONS:
        return preserve(failure(code=code, service=service, retry_after_seconds=value.get('retry_after_seconds')))
    if value.get('connected') is False:
        return preserve(failure(service=service))
    return None
