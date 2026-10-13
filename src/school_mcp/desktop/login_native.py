"""Official-login job bridge; browser, verification and session IO live in Rust."""
import time

from . import native_core
from ..mail.config import local_dir
from ..service_errors import ServiceError


def run(service, notify, stopped=None, timeout=600, *, headed=True):
    value = native_core.call('login_begin', {
        'private_dir': str(local_dir()), 'service': service, 'headed': headed,
        'timeout_seconds': timeout,
    })
    identifier = value['job_id']
    last, cancel_sent = None, False
    try:
        while True:
            if stopped is not None and stopped.is_set() and not cancel_sent:
                native_core.call('login_cancel', {'job_id': identifier})
                cancel_sent = True
            status = native_core.call('login_status', {'job_id': identifier})
            progress = (status.get('stage'), status.get('detail'))
            if all(progress) and progress != last:
                notify(*progress)
                last = progress
            state = status.get('state')
            if state == 'succeeded':
                return status['result']
            if state in {'failed', 'cancelled'}:
                error = status.get('error') or {}
                raise ServiceError(error.get('message') or '登录未完成；已有会话保留。',
                                   code=error.get('code') or state)
            if state not in {'pending', 'running'}:
                raise ServiceError('原生登录任务返回状态异常；没有自动重试登录。', code='unavailable')
            if stopped is None:
                time.sleep(.15)
            elif not cancel_sent:
                stopped.wait(.15)
            else:
                time.sleep(.15)
    except BaseException:
        if not cancel_sent:
            try:
                native_core.call('login_cancel', {'job_id': identifier})
            except Exception:
                pass
        raise
