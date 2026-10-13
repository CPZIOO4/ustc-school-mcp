"""Private-pipe bridge for fixed authenticated reads; no Python network fallback."""
import math
import time

import httpx

from .desktop import native_core
from .desktop.model import ModelError
from .mail.config import local_dir


def request(service, path, cookies, user_agent, error_type, params=None, method='GET'):
    try:
        database = str(local_dir() / 'request-policy.sqlite3')
    except OSError:
        raise error_type('本机请求节流状态不可用，请检查私人目录权限。', code='local_policy_unavailable') from None
    value = dict(service=service, path=path, cookies=cookies, user_agent=user_agent or 'SchoolMCP/0.1',
                 params=params or {}, method=method, database=database)
    deadline = time.monotonic() + 10
    try:
        while True:
            result = native_core.call('authenticated_read', value)
            state = result.get('state')
            if state == 'response':
                # This is only an in-memory compatibility container. Network, redirects,
                # cookies and response decoding have already run in Rust.
                return httpx.Response(result['status'], headers=result['headers'],
                                      text=result['text'], request=httpx.Request(method, result['url']))
            if state in {'wait', 'cooldown'}:
                delay = result.get('retry_after_seconds')
                if type(delay) not in (int, float) or not math.isfinite(delay) or delay < 0:
                    raise error_type('本机网络策略返回无效状态。', code='local_policy_unavailable')
                if state == 'wait' and time.monotonic() + delay <= deadline:
                    # Rust emits wait only before the first request is sent.
                    time.sleep(delay)
                    continue
                raise error_type('站点处于限速或失败退避期，请稍后重试。', code='cooldown',
                                 retry_after_seconds=max(1, math.ceil(delay)))
            if state == 'error':
                raise error_type(result.get('message', '学校查询失败。'), code=result.get('code', 'unavailable'))
            raise error_type('Rust 查询返回无效状态。', code='runtime_failure')
    except ModelError as exc:
        raise error_type(str(exc), code=exc.code) from None
