"""Immutable DPAPI mail drafts and a durable, cross-process one-attempt send gate."""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import smtplib
import sqlite3
import ssl
import uuid
from contextlib import contextmanager
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from pathlib import Path

from .. import network
from .config import MailConfig, MailError, local_dir
from .credentials import _dpapi, load_password

MAX_BYTES = 20 * 1024 * 1024  # Local MIME limit, not a claim about server limits.
ADDRESS = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}\Z")
MESSAGE_ID = re.compile(r"<[^\s<>\x00-\x1f\x7f]+@[^\s<>\x00-\x1f\x7f]+>\Z")


def valid_address(value: str) -> bool:
    if not isinstance(value, str) or len(value) > 254 or not ADDRESS.fullmatch(value):
        return False
    local, domain = value.rsplit('@', 1)
    return (len(local) <= 64 and not local.startswith('.') and not local.endswith('.')
            and '..' not in value and all(len(x) <= 63 and not x.startswith('-')
                                          and not x.endswith('-') for x in domain.split('.')))


class Outbox:
    def __init__(self, directory: Path | None = None):
        self.path = (directory or local_dir()) / 'mail-outbox.sqlite3'

    @contextmanager
    def database(self):
        connection = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5)
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, state TEXT NOT NULL, payload BLOB NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS sent_copies (draft_id TEXT PRIMARY KEY, state TEXT NOT NULL, payload BLOB NOT NULL)')
            yield connection
            connection.commit()
        except (OSError, sqlite3.Error):
            raise MailError('本地发件记录不可用；请检查私人目录，不要重新发送结果不明的邮件。') from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def encode(payload: dict) -> bytes:
        return _dpapi(json.dumps(payload, ensure_ascii=False).encode('utf-8'))

    def create(self, payload: dict) -> str:
        identifier = uuid.uuid4().hex
        encrypted = self.encode(payload)
        with self.database() as db:
            db.execute('INSERT INTO drafts VALUES (?, ?, ?)', (identifier, 'ready', encrypted))
        return identifier

    def get(self, identifier: str) -> tuple[str, dict]:
        if not re.fullmatch(r'[0-9a-f]{32}', identifier):
            raise MailError('草稿编号无效，请使用准备邮件工具返回的 draft_id。')
        with self.database() as db:
            row = db.execute('SELECT state, payload FROM drafts WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise MailError('未找到本机邮件草稿。')
        try:
            return row[0], json.loads(_dpapi(row[1], decrypt=True))
        except (ValueError, TypeError):
            raise MailError('发件记录无法解密或已损坏；不要重新发送结果不明的邮件。') from None

    def claim(self, identifier: str) -> bool:
        # Commit before touching the SMTP server. A crash leaves a non-retryable state.
        with self.database() as db:
            changed = db.execute("UPDATE drafts SET state='sending' WHERE id=? AND state='ready'", (identifier,)).rowcount
        return changed == 1

    def finish(self, identifier: str, state: str, payload: dict) -> None:
        encrypted = self.encode(payload)
        with self.database() as db:
            db.execute('UPDATE drafts SET state=?, payload=? WHERE id=?', (state, encrypted, identifier))

    def copy_attempt(self, identifier: str) -> dict | None:
        with self.database() as db:
            row = db.execute('SELECT state, payload FROM sent_copies WHERE draft_id=?', (identifier,)).fetchone()
        if row is None:
            return None
        return {'state': row[0], **json.loads(_dpapi(row[1], decrypt=True))}

    def claim_copy(self, identifier: str, mailbox: str) -> bool:
        encrypted = self.encode({'mailbox': mailbox})
        with self.database() as db:
            changed = db.execute("INSERT OR IGNORE INTO sent_copies VALUES (?, 'archiving', ?)",
                                 (identifier, encrypted)).rowcount
        return changed == 1

    def finish_copy(self, identifier: str, state: str) -> None:
        with self.database() as db:
            db.execute('UPDATE sent_copies SET state=? WHERE draft_id=?', (state, identifier))


def prepare(config: MailConfig, *, to: list[str], subject: str, body: str,
            cc: list[str] | None = None, bcc: list[str] | None = None,
            attachments: list[str] | None = None, reply_headers: dict | None = None,
            store: Outbox | None = None) -> dict:
    """No network. Freeze MIME and attachment bytes before returning the preview."""
    fields = {'to': to, 'cc': cc or [], 'bcc': bcc or []}
    issues = []
    if not to:
        issues.append({'field': 'to', 'reason': 'required'})
    for field, values in fields.items():
        if not isinstance(values, list) or any(not valid_address(value) for value in values):
            issues.append({'field': field, 'reason': 'use_list_of_bare_email_addresses'})
    if sum(len(v) for v in fields.values() if isinstance(v, list)) > 20:
        issues.append({'field': 'recipients', 'reason': 'maximum_20'})
    if not subject.strip() or len(subject) > 500 or any(ord(c) < 32 or ord(c) == 127 for c in subject):
        issues.append({'field': 'subject', 'reason': 'required_single_line_maximum_500'})
    if not body.strip() or len(body) > 100000 or '\x00' in body:
        issues.append({'field': 'body', 'reason': 'required_text_maximum_100000'})
    paths = attachments or []
    if not isinstance(paths, list) or len(paths) > 10:
        issues.append({'field': 'attachments', 'reason': 'maximum_10_explicit_file_paths'})
    if issues:
        return {'status': 'needs_input', 'issues': issues, 'next_action': 'correct_fields_then_prepare', 'sent': False}
    message = EmailMessage(policy=policy.SMTP)
    message['From'] = config.address
    message['To'] = ', '.join(to)
    if cc:
        message['Cc'] = ', '.join(cc)
    # Bcc participates only in the envelope, never in serialized MIME headers.
    message['Subject'] = subject
    message['Date'] = formatdate(localtime=False)
    message['Message-ID'] = make_msgid(domain=config.address.split('@')[1])
    if reply_headers:
        for field in ('In-Reply-To', 'References'):
            value = reply_headers.get(field, '')
            if value:
                if len(value) > 900 or not all(MESSAGE_ID.fullmatch(x) for x in value.split()):
                    raise MailError('原邮件的线程头无效，不能自动构造回复。')
                message[field] = value
    message.set_content(body)
    attachment_info = []
    total = 0
    for index, filename in enumerate(paths):
        try:
            path = Path(filename)
            if not path.is_absolute() or not path.is_file():
                raise ValueError
            with path.open('rb') as stream:
                data = stream.read(MAX_BYTES - total + 1)
            total += len(data)
            if total > MAX_BYTES or any(ord(c) < 32 or ord(c) == 127 for c in path.name):
                raise ValueError
            mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            main, sub = mime.split('/', 1)
            message.add_attachment(data, maintype=main, subtype=sub, filename=path.name)
            attachment_info.append({'filename': path.name, 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        except (OSError, ValueError, TypeError):
            return {'status': 'needs_input', 'issues': [{'field': f'attachments[{index}]', 'reason': 'unreadable_or_too_large_file'}], 'next_action': 'correct_fields_then_prepare', 'sent': False}
    raw = message.as_bytes()
    if len(raw) > MAX_BYTES:
        return {'status': 'needs_input', 'issues': [{'field': 'attachments', 'reason': 'MIME_exceeds_20_MiB'}], 'next_action': 'reduce_message_size_then_prepare', 'sent': False}
    payload = {'account': config.address, 'recipients': list(dict.fromkeys(to + (cc or []) + (bcc or []))),
               'raw': base64.b64encode(raw).decode(), 'sha256': hashlib.sha256(raw).hexdigest(),
               'message_id': str(message['Message-ID']),
               'preview': {**fields, 'from': config.address, 'subject': subject, 'body': body,
                           'attachments': attachment_info, 'reply_to_message_id': (reply_headers or {}).get('In-Reply-To')}}
    store = store or Outbox()
    identifier = store.create(payload)
    return receipt(identifier, 'ready', payload, include_preview=True)


def receipt(identifier: str, state: str, payload: dict, *, include_preview: bool = False) -> dict:
    # 'sending' may mean a live attempt OR an interrupted process. Never auto-resend.
    actions = {'ready': 'send_only_if_user_authorized', 'sending': 'check_status_do_not_resend',
               'accepted': 'find_replies_or_report_server_acceptance', 'partial': 'review_refused_recipients_do_not_resend_all',
               'rejected': 'correct_issue_then_prepare_new_draft', 'failed_before_data': 'resolve_error_then_prepare_new_draft',
               'unknown': 'verify_with_recipient_do_not_resend'}
    result = {'draft_id': identifier, 'status': state, 'content_sha256': payload['sha256'],
              'message_id': payload['message_id'], 'next_action': actions[state],
              'server_accepted': state in {'accepted', 'partial'}, 'delivery_confirmed': False,
              'can_send': state == 'ready', 'sent_folder_copy': 'check_with_school_mail_check_sent_copy'}
    if 'result' in payload:
        result.update(payload['result'])
    if include_preview:
        preview = dict(payload['preview'])
        preview['body'] = preview['body'][:6000]
        preview['body_truncated'] = len(payload['preview']['body']) > 6000
        result['preview'] = preview
        result['content_is_untrusted'] = True
    return result


def status(identifier: str, *, include_preview: bool = False, store: Outbox | None = None) -> dict:
    state, payload = (store or Outbox()).get(identifier)
    return receipt(identifier, state, payload, include_preview=include_preview)


def send(config: MailConfig, identifier: str, content_sha256: str, *, store: Outbox | None = None) -> dict:
    store = store or Outbox()
    state, payload = store.get(identifier)
    if config.address != payload['account']:
        raise MailError('当前邮箱与草稿发件账号不一致，已停止发送。')
    if content_sha256 != payload['sha256']:
        raise MailError('邮件校验摘要不一致，请重新查看草稿。')
    if state != 'ready':
        return receipt(identifier, state, payload)
    raw = base64.b64decode(payload['raw'], validate=True)
    if hashlib.sha256(raw).hexdigest() != content_sha256:
        raise MailError('草稿内容校验失败，已停止发送。')
    # Resolve local prerequisites before consuming the one-attempt gate.
    password = load_password(config)
    try:
        network.limiter.acquire('smtp')
    except network.CooldownError as exc:
        return {**receipt(identifier, 'ready', payload), 'next_action': 'wait_then_send_same_draft',
                'retry_after_seconds': exc.retry_after_seconds}
    if not store.claim(identifier):
        return status(identifier, store=store)
    connection = None
    stage = 'connect'
    accepted, refused = [], []
    final_state = 'failed_before_data'
    result = {}
    try:
        connection = smtplib.SMTP_SSL('mail.ustc.edu.cn', 465, timeout=config.timeout, context=ssl.create_default_context())
        connection.ehlo_or_helo_if_needed()
        stage = 'authenticate'
        # Don't try several mechanisms/passwords after an authentication refusal.
        connection.user, connection.password = config.address, password
        mechanisms = connection.esmtp_features.get('auth', '').upper().split()
        if 'PLAIN' in mechanisms:
            connection.auth('PLAIN', connection.auth_plain)
        elif 'LOGIN' in mechanisms:
            connection.auth('LOGIN', connection.auth_login)
        else:
            raise smtplib.SMTPNotSupportedError('No supported authentication mechanism')
        stage = 'envelope'
        code, _ = connection.mail(config.address)
        if code != 250:
            final_state = 'rejected'
            result = {'error_code': 'sender_rejected', 'smtp_code': code}
        else:
            for address in payload['recipients']:
                code, _ = connection.rcpt(address)
                if code in (250, 251):
                    accepted.append(address)
                else:
                    refused.append({'address': address, 'smtp_code': code})
            if not accepted:
                final_state = 'rejected'
                result = {'error_code': 'all_recipients_refused', 'refused_recipients': refused}
            else:
                # All exceptions after entering DATA are conservatively unknown except
                # an explicit SMTPDataError (server negative response).
                stage = 'data'
                code, _ = connection.data(raw)
                if code == 250:
                    final_state = 'partial' if refused else 'accepted'
                    result = {'accepted_recipients': accepted, 'refused_recipients': refused}
                else:
                    final_state = 'rejected'
                    result = {'error_code': 'data_rejected', 'smtp_code': code}
    except smtplib.SMTPAuthenticationError:
        result = {'error_code': 'authentication_failed'}
    except smtplib.SMTPDataError as exc:
        final_state = 'rejected'
        result = {'error_code': 'data_rejected', 'smtp_code': exc.smtp_code}
    except (smtplib.SMTPException, OSError, ValueError):
        final_state = 'unknown' if stage == 'data' else 'failed_before_data'
        result = {'error_code': 'connection_or_protocol_error', 'failed_stage': stage}
    finally:
        if connection is not None:
            # No QUIT result can undo a positive DATA reply.
            try:
                connection.close()
            except OSError:
                pass
    payload['result'] = result
    try:
        store.finish(identifier, final_state, payload)
    except MailError:
        return {**receipt(identifier, 'unknown', payload), 'error_code': 'receipt_persistence_failed'}
    try:
        if final_state in {'accepted', 'partial'}:
            network.limiter.success('smtp')
        else:
            network.limiter.failure('smtp', authentication=stage == 'authenticate')
    except network.PolicyError:
        # Sending already finished; a pacing database fault must never imply retry.
        result['pacing_warning'] = 'local_policy_unavailable'
    return receipt(identifier, final_state, payload)


def prepare_reply(client, *, uid: int, uid_validity: int, body: str, mailbox: str = 'INBOX',
                  attachments: list[str] | None = None, store: Outbox | None = None) -> dict:
    original = client.read(uid, uid_validity, mailbox=mailbox, max_chars=1, include_headers=True)
    target = original.get('reply-to') or original['from']
    addresses = getaddresses([target])
    if len(addresses) != 1 or not valid_address(addresses[0][1]):
        return {'status': 'needs_input', 'issues': [{'field': 'to', 'reason': 'ambiguous_reply_address'}],
                'next_action': 'ask_user_for_recipient_then_prepare', 'sent': False}
    message_id = original.get('message-id', '').strip()
    if not MESSAGE_ID.fullmatch(message_id):
        return {'status': 'needs_input', 'issues': [{'field': 'message_id', 'reason': 'invalid_original_thread_header'}],
                'next_action': 'prepare_new_message_if_user_agrees', 'sent': False}
    subject = original['subject']
    if not subject.lower().startswith('re:'):
        subject = 'Re: ' + subject
    # Only validated parent ID; do not copy unbounded or malformed external chains.
    result = prepare(client.config, to=[addresses[0][1]], subject=subject, body=body, attachments=attachments,
                     reply_headers={'In-Reply-To': message_id, 'References': message_id}, store=store)
    result['reply_source'] = {'mailbox': mailbox, 'uid': uid, 'uid_validity': uid_validity,
                              'uses_reply_to_header': bool(original.get('reply-to'))}
    return result
