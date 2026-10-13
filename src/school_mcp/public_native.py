"""Temporary synchronous IO bridge to the Rust public-site adapters."""
import math
import time

from .desktop import native_core
from .desktop.model import ModelError
from .mail.config import local_dir


def enabled(transport):
    # Explicit test transports exercise the retained Python implementation.
    return transport is None and native_core.enabled()


def read(action, params, error_type):
    try:
        directory = local_dir()
    except OSError:
        raise error_type('本机请求节流状态不可用，请检查私人目录权限。', code='local_policy_unavailable') from None
    # Rust owns directory creation and maps storage failures to the fixed policy code.
    request = {'action': action, 'params': params, 'database': str(directory / 'request-policy.sqlite3')}
    deadline = time.monotonic() + 10
    try:
        while True:
            result = native_core.call('public_read', request)
            state = result.get('state')
            if state == 'parsed':
                return result['result']
            if state in {'wait', 'cooldown'}:
                delay = result.get('retry_after_seconds')
                if not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 0:
                    raise error_type('本机网络策略返回无效状态。', code='local_policy_unavailable')
                if state == 'wait' and time.monotonic() + delay <= deadline:
                    # No request has been sent; reacquire the shared reservation after sleeping.
                    time.sleep(delay)
                    continue
                raise error_type('站点处于限速或失败退避期，请稍后重试。', code='cooldown', retry_after_seconds=max(1, math.ceil(delay)))
            if state == 'error':
                raise error_type(result.get('message', '公共页面读取失败。'), code=result.get('code', 'unavailable'))
            raise error_type('Rust 站点适配器返回无效状态。', code='runtime_failure')
    except ModelError as exc:
        raise error_type(str(exc), code=exc.code) from None
