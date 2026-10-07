"""Locate sent copies, then append at most once with a separate durable gate."""
from __future__ import annotations

import base64
import hashlib
import re

from .client import _ok
from .config import MailError
from .outbox import Outbox
from .parsing import encode_mailbox, parse_email, parse_list_item, quote


def _folder(connection, requested: str) -> tuple[str | None, dict | None]:
    folders = [parse_list_item(item) for item in _ok(*connection.list(), '查询发送文件夹') if item]
    selectable = [f for f in folders if f['selectable']]
    if requested:
        if any(f['name'] == requested for f in selectable):
            return requested, None
        return None, {'status': 'needs_mailbox', 'next_action': 'choose_existing_selectable_folder',
                      'folders': [f['name'] for f in selectable]}
    sent = [f['name'] for f in selectable if any(flag.lower() == '\\sent' for flag in f['flags'])]
    if len(sent) != 1:
        return None, {'status': 'needs_mailbox', 'next_action': 'ask_user_to_choose_sent_folder',
                      'folders': sent or [f['name'] for f in selectable]}
    return sent[0], None


def _find(client, connection, mailbox: str, message_id: str) -> dict:
    validity, _ = client.select(connection, mailbox)
    data = _ok(*connection.uid('SEARCH', 'HEADER', 'Message-ID', quote(message_id)), '查询发送副本')
    identifiers = (data[0] or b'').split()
    found = []
    for identifier in identifiers[:20]:
        fetched = _ok(*connection.uid('FETCH', identifier,
                         '(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])'), '核对发送副本')
        for item in fetched:
            if not isinstance(item, tuple):
                continue
            metadata, raw = item
            uid_match = re.search(rb'UID (\d+)', metadata)
            if not uid_match or int(uid_match.group(1)) != int(identifier):
                raise MailError('发送副本编号与请求不一致。')
            if parse_email(raw, include_body=False)['message-id'].strip() == message_id:
                found.append({'uid': int(identifier), 'uid_validity': validity, 'mailbox': mailbox})
    truncated = len(identifiers) > 20
    return {'status': 'present' if found else ('incomplete' if truncated else 'missing'),
            'mailbox': mailbox, 'copies': found, 'copy_count': len(found), 'truncated': truncated,
            'match_basis': 'exact_message_id', 'delivery_confirmed': False,
            'next_action': 'use_existing_copy' if found else ('inspect_manually' if truncated else 'save_copy_if_sent_and_authorized')}


def _draft(client, store: Outbox, identifier: str) -> tuple[str, dict]:
    state, payload = store.get(identifier)
    if payload['account'] != client.config.address:
        raise MailError('当前邮箱与草稿发件账号不一致。')
    return state, payload


def check(client, identifier: str, mailbox: str = '', *, store: Outbox | None = None) -> dict:
    store = store or Outbox()
    state, payload = _draft(client, store, identifier)
    attempt = store.copy_attempt(identifier)
    # Follow the folder of a previous explicit save unless the caller selects another.
    requested = mailbox or (attempt or {}).get('mailbox', '')
    with client.session() as connection:
        resolved, error = _folder(connection, requested)
        result = error or _find(client, connection, resolved, payload['message_id'])
    result.update(draft_id=identifier, send_status=state, copy_attempt=attempt)
    if result['status'] == 'missing' and (attempt or state not in {'accepted', 'partial'}):
        result['next_action'] = 'inspect_manually_do_not_append_or_resend'
    # This is intentionally a live, read-only view; a copy alone does not prove delivery.
    return result


def save(client, identifier: str, mailbox: str = '', *, store: Outbox | None = None) -> dict:
    store = store or Outbox()
    state, payload = _draft(client, store, identifier)
    if state not in {'accepted', 'partial'}:
        return {'status': 'not_eligible', 'send_status': state, 'draft_id': identifier,
                'next_action': 'resolve_send_status_do_not_archive', 'append_attempted': False}
    raw = base64.b64decode(payload['raw'], validate=True)
    if hashlib.sha256(raw).hexdigest() != payload['sha256']:
        raise MailError('草稿内容校验失败，已停止保存副本。')
    attempt = store.copy_attempt(identifier)
    requested = mailbox or (attempt or {}).get('mailbox', '')
    append_attempted = False
    outcome = None
    try:
        with client.session() as connection:
            resolved, error = _folder(connection, requested)
            if error:
                return {**error, 'draft_id': identifier, 'append_attempted': False}
            result = _find(client, connection, resolved, payload['message_id'])
            if result['status'] != 'missing':
                return {**result, 'draft_id': identifier, 'append_attempted': False}
            if not store.claim_copy(identifier, resolved):
                return {**result, 'status': 'previous_attempt', 'draft_id': identifier,
                        'copy_attempt': store.copy_attempt(identifier), 'append_attempted': False,
                        'next_action': 'check_sent_copy_do_not_append_or_resend'}
            # The local gate is committed before APPEND. A crash never enables replay.
            append_attempted = True
            code, _ = connection.append(quote(encode_mailbox(resolved)), '(\\Seen)', None, raw)
            outcome = 'saved' if code == 'OK' else 'rejected'
            store.finish_copy(identifier, outcome)
            if outcome == 'rejected':
                return {'status': 'rejected', 'draft_id': identifier, 'mailbox': resolved,
                        'append_attempted': True, 'next_action': 'inspect_manually_do_not_append_or_resend'}
            # A successful APPEND plus fresh lookup proves only that a mailbox copy exists.
            result = _find(client, connection, resolved, payload['message_id'])
            return {**result, 'status': 'saved' if result['status'] == 'present' else 'saved_unverified',
                    'draft_id': identifier, 'append_attempted': True,
                    'next_action': 'use_existing_copy' if result['status'] == 'present' else 'check_sent_copy_do_not_append_or_resend'}
    except MailError:
        if not append_attempted:
            raise
        # Covers a lost APPEND reply, local persistence failure, and post-save lookup error.
        if outcome is None:
            try:
                store.finish_copy(identifier, 'unknown')
            except MailError:
                pass
        return {'status': 'saved_unverified' if outcome == 'saved' else 'unknown',
                'draft_id': identifier, 'append_attempted': True,
                'next_action': 'check_sent_copy_do_not_append_or_resend', 'delivery_confirmed': False}
