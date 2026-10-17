"""Private native mailbox workflow bridge. No credential appears in job results."""
import time
from email.utils import getaddresses

from ..desktop import native_core
from ..desktop.model import ModelError
from .config import MailError, local_dir


def parse(action, **values):
    try:
        return native_core.call('mail_parse', {'action': action, **values})
    except ModelError as exc:
        raise MailError(str(exc)) from None


def envelope(message, address):
    # RFC header syntax parsing remains in Python until mail protocol migration.
    return {'address': address, 'message': message,
            'senders': [a for _, a in getaddresses([message.get('from', '')])],
            'recipients': [a for _, a in getaddresses([message.get('to', ''), message.get('cc', '')])]}


def bind(*, headed, create_client_password, resume, address, replace_existing, timeout, notify, stopped=None):
    from . import browser_setup
    # Preserve replacement identity semantics without passing secrets to the job.
    if not address and replace_existing:
        try:
            address = browser_setup.load_config().address
        except MailError:
            pass
    identifier = None
    terminal = False
    cancel_sent = False
    try:
        value = native_core.call('mail_bind_begin', {
            'private_dir': str(local_dir()), 'headed': headed,
            'create_client_password': create_client_password, 'resume': resume,
            'address': address, 'replace_existing': replace_existing,
            'timeout_seconds': timeout,
        })
        identifier = value['job_id']
        last = None
        while True:
            if stopped is not None and stopped.is_set() and not cancel_sent:
                native_core.call('mail_bind_cancel', {'job_id': identifier})
                cancel_sent = True
            value = native_core.call('mail_bind_status', {'job_id': identifier})
            progress = value.get('progress')
            if progress and progress != last:
                progress = {**progress, 'next_step': browser_setup.NEXT.get(progress.get('state'), '')}
                notify(progress)
                last = value.get('progress')
            if value['state'] == 'succeeded':
                terminal = True
                result = value['result']
                if result['state'] == 'captured':
                    # IMAP check/save is the remaining legacy operation. Re-read
                    # the durable journal under the old byte-0 lock after Rust exits.
                    with browser_setup.bind_lock():
                        pending = browser_setup._load()
                        if address and pending.get('address', '').lower() != address.lower():
                            raise MailError('暂存密码与指定邮箱不匹配；停止处理。')
                        return browser_setup.finish_pending(pending)
                return {**result, 'next_step': browser_setup.NEXT.get(result['state'], '')}
            if value['state'] in {'failed', 'cancelled'}:
                terminal = True
                raise MailError((value.get('error') or {}).get('message') or '邮箱绑定未完成；原生成记录保留。')
            if value['state'] not in {'pending', 'running'}:
                raise MailError('邮箱绑定状态异常；没有自动重复生成密码。')
            time.sleep(.15)
    except ModelError as exc:
        raise MailError(str(exc)) from None
    finally:
        if identifier and not terminal and not cancel_sent:
            try:
                native_core.call('mail_bind_cancel', {'job_id': identifier})
            except Exception:
                pass
