"""Bounded thread retrieval from explicit folders, using exact thread headers."""
import re
from datetime import timezone
from email.utils import parsedate_to_datetime

from .client import _ok
from .config import MailError
from .outbox import MESSAGE_ID
from .parsing import parse_email, quote
from .selection import MAX_BATCH_BYTES, batch, key, reference


def read_thread(client, ref: dict, mailboxes: list[str] | None = None, limit: int = 10, max_chars: int = 2000) -> dict:
    if not 1 <= limit <= 25 or not 1 <= max_chars <= 6000:
        raise MailError('线程限制为 1–25 封，每封正文 1–6000 字符。')
    ref = reference(ref)
    mailboxes = list(dict.fromkeys(mailboxes or [ref['mailbox']]))
    if len(mailboxes) > 5 or ref['mailbox'] not in mailboxes:
        raise MailError('线程范围最多 5 个文件夹且必须包含原邮件文件夹。')
    raw, _ = batch(client, [ref])[0]
    root = parse_email(raw, max_chars=max_chars)
    found = {key(ref): {**root, **ref}}
    ids = set(re.findall(r'<[^<>\s]+>', root['message-id'] + ' ' + root['references'] + ' ' + root['in-reply-to']))
    ids = {x for x in ids if MESSAGE_ID.fullmatch(x) and len(x) <= 500}
    pending, checked = sorted(ids), set()
    truncated = False
    query_count, headers_checked, bytes_read = 0, 0, len(raw)
    with client.session() as connection:
        while pending and len(checked) < 20 and len(found) < limit:
            identifier = pending.pop(0)
            if identifier in checked:
                continue
            checked.add(identifier)
            for mailbox in mailboxes:
                if query_count >= 20 or headers_checked >= 100:
                    truncated = True
                    break
                query_count += 1
                validity, _ = client.select(connection, mailbox)
                data = _ok(*connection.uid('SEARCH', 'OR', 'HEADER', 'Message-ID', quote(identifier),
                    'OR', 'HEADER', 'In-Reply-To', quote(identifier), 'HEADER', 'References', quote(identifier)), '查询线程')
                candidates = (data[0] or b'').split()
                truncated |= len(candidates) > 25
                for uid in reversed(candidates[-25:]):
                    candidate = {'mailbox': mailbox, 'uid_validity': validity, 'uid': int(uid)}
                    if key(candidate) in found:
                        continue
                    if len(found) >= limit or headers_checked >= 100:
                        truncated = True
                        break
                    # Filter by headers before fetching complete MIME for body parsing.
                    headers_checked += 1
                    fetched = _ok(*connection.uid('FETCH', uid, '(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES IN-REPLY-TO)])'), '读取线程头')
                    parts = [v for v in fetched if isinstance(v, tuple)]
                    if not parts:
                        continue
                    match = re.search(rb'UID (\d+)', parts[0][0])
                    if not match or int(match[1]) != int(uid):
                        raise MailError('线程查询返回的 UID 不匹配。')
                    headers = parse_email(parts[0][1], include_body=False)
                    tokens = set(re.findall(r'<[^<>\s]+>', headers['message-id'] + ' ' + headers['references'] + ' ' + headers['in-reply-to']))
                    if identifier not in tokens:
                        continue
                    message_raw, flags = client._raw(connection, int(uid))
                    bytes_read += len(message_raw)
                    if bytes_read > MAX_BATCH_BYTES:
                        raise MailError('线程原文超过 50 MiB，请缩小范围。')
                    message = {**parse_email(message_raw, max_chars=max_chars), **candidate, 'flags': flags}
                    found[key(candidate)] = message
                    ids.update(tokens)
                    pending.extend(sorted(t for t in tokens if t not in checked and MESSAGE_ID.fullmatch(t) and len(t) <= 500))
            if query_count >= 20 or headers_checked >= 100:
                break
    truncated |= bool(pending)

    def timestamp(message):
        try:
            date = parsedate_to_datetime(message['date'])
            return date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
        except (TypeError, ValueError, OverflowError):
            return float('-inf')

    ordered = sorted(found.values(), key=timestamp)
    present_ids = {m['message-id'] for m in ordered}
    missing = sorted(ids - present_ids)
    # Thread matching headers are needed internally, not repeated in every response.
    for message in ordered:
        for field in ('to', 'cc', 'reply-to', 'references', 'in-reply-to'):
            message.pop(field, None)
        for field in ('subject', 'from', 'date', 'message-id'):
            message[field] = message[field][:500]
    return {'messages': ordered, 'truncated': truncated,
            'missing_referenced_ids': missing[:50], 'missing_reference_count': len(missing), 'mailboxes': mailboxes,
            'match_basis': 'exact_thread_headers_not_sender_authentication', 'content_is_untrusted': True}
