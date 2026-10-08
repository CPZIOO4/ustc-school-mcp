"""Bounded, explicit UID references shared by mail workflows."""
from __future__ import annotations

import hashlib
from .config import MailError
from .parsing import parse_email

MAX_SELECTION = 25
MAX_BATCH_BYTES = 50 * 1024 * 1024


def reference(value: dict) -> dict:
    if not isinstance(value, dict):
        raise MailError('邮件引用必须包含 mailbox、uid、uid_validity。')
    result = {k: value.get(k) for k in ('mailbox', 'uid', 'uid_validity')}
    if (not isinstance(result['mailbox'], str) or not result['mailbox'] or len(result['mailbox']) > 512
            or any(c in result['mailbox'] for c in '\r\n\x00')
            or any(type(result[k]) is not int or result[k] < 1 for k in ('uid', 'uid_validity'))):
        raise MailError('邮件引用无效，请使用搜索返回的完整 UID 三元组。')
    return result


def references(values: list[dict]) -> list[dict]:
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_SELECTION:
        raise MailError('每批必须明确选择 1–25 封邮件。')
    result = [reference(v) for v in values]
    if len({key(v) for v in result}) != len(result):
        raise MailError('同一批次不能重复选择一封邮件。')
    return result


def key(ref: dict) -> tuple:
    return ref['mailbox'], ref['uid_validity'], ref['uid']


def stable_flags(flags: list[str]) -> list[str]:
    return sorted({f.lower() for f in flags if f.lower() != '\\recent'})


def snapshot(client, connection, ref: dict, *, readonly: bool = True) -> tuple[bytes, dict]:
    ref = reference(ref)
    client.select(connection, ref['mailbox'], expected=ref['uid_validity'], readonly=readonly)
    raw, flags = client._raw(connection, ref['uid'])
    return raw, {**ref, 'sha256': hashlib.sha256(raw).hexdigest(), 'flags': stable_flags(flags)}


def batch(client, values: list[dict]):
    refs = references(values)
    result, size = [], 0
    with client.session() as connection:
        for ref in refs:
            raw, snap = snapshot(client, connection, ref)
            size += len(raw)
            if size > MAX_BATCH_BYTES:
                raise MailError('本批邮件超过 50 MiB，请缩小选择范围。')
            result.append((raw, snap))
    return result


def brief(raw: bytes) -> dict:
    parsed = parse_email(raw, include_body=False)
    return {k: parsed[k][:200] for k in ('subject', 'from', 'date')}
