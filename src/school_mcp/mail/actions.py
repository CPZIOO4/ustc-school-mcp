"""Encrypted, frozen mail action plans; no global EXPUNGE or automatic retries."""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from . import outbox
from .client import _ok
from .config import MailError
from .parsing import encode_mailbox, parse_list_item, quote
from .selection import batch, brief, key, reference, snapshot

ARCHIVE_ROOT = '归档文件夹'


def folders(connection) -> list[dict]:
    return [parse_list_item(v) for v in _ok(*connection.list(), '查询文件夹') if v]


def archive_target(items: list[dict], categories: list[str] | None = None) -> str:
    categories = categories or []
    if not categories:
        return ARCHIVE_ROOT
    delimiters = {f['delimiter'] for f in items if f['name'] == ARCHIVE_ROOT}
    if not delimiters:
        delimiters = {f['delimiter'] for f in items}
    if len(delimiters) != 1 or None in delimiters or '' in delimiters:
        raise MailError('无法确定归档目录层级分隔符，不能创建子类别。')
    delimiter = next(iter(delimiters))
    if len(categories) > 3 or any(not isinstance(c, str) or not c.strip() or len(c) > 80
            or delimiter in c or any(x in c for x in '\r\n\x00') for c in categories):
        raise MailError('类别最多三级，每一级必须是独立名称。')
    return delimiter.join([ARCHIVE_ROOT] + categories)


def is_archive(name: str, items: list[dict]) -> bool:
    if name == ARCHIVE_ROOT:
        return True
    roots = [f for f in items if f['name'] == ARCHIVE_ROOT]
    if not roots:
        roots = [f for f in items if f['name'] == name]
    return any(f['delimiter'] and name.startswith(ARCHIVE_ROOT + f['delimiter']) for f in roots)


def create_folder(client, name: str = '', categories: list[str] | None = None, archive: bool = False) -> dict:
    with client.session() as connection:
        listed = folders(connection)
        if archive:
            name = archive_target(listed, categories)
        reference({'mailbox': name, 'uid': 1, 'uid_validity': 1})
        existing = next((f for f in listed if f['name'] == name), None)
        if existing:
            return {'status': 'exists', 'folder': existing, 'is_archive': is_archive(name, listed)}
        # CREATE is name-idempotent: after timeout inspect LIST before calling again.
        _ok(*connection.create(quote(encode_mailbox(name))), '创建指定文件夹')
        listed = folders(connection)
        created = next((f for f in listed if f['name'] == name), None)
        return {'status': 'created' if created else 'unverified', 'folder': created,
                'is_archive': is_archive(name, listed), 'next_action': 'list_folders_if_unverified'}


class Plans(outbox.Outbox):
    def _table(self, db):
        db.execute('CREATE TABLE IF NOT EXISTS mail_plans (id TEXT PRIMARY KEY, state TEXT NOT NULL, payload BLOB NOT NULL)')

    def create_plan(self, payload):
        identifier = uuid.uuid4().hex
        payload['digest'] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        encrypted = self.encode(payload)
        with self.database() as db:
            self._table(db)
            db.execute('INSERT INTO mail_plans VALUES (?, ?, ?)', (identifier, 'ready', encrypted))
        return identifier

    def get_plan(self, identifier):
        if not re.fullmatch('[0-9a-f]{32}', identifier):
            raise MailError('操作计划编号无效。')
        with self.database() as db:
            self._table(db)
            row = db.execute('SELECT state,payload FROM mail_plans WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise MailError('找不到本机操作计划。')
        return row[0], json.loads(outbox._dpapi(row[1], decrypt=True))

    def save_plan(self, identifier, state, payload):
        encrypted = self.encode(payload)
        with self.database() as db:
            self._table(db)
            db.execute('UPDATE mail_plans SET state=?,payload=? WHERE id=?', (state, encrypted, identifier))

    def claim_plan(self, identifier, payload):
        with self.database() as db:
            self._table(db)
            db.execute('BEGIN IMMEDIATE')
            # Block overlapping interrupted operations even through newly prepared plans.
            wanted = {key(i['before']) for i in payload['items']}
            for old_id, raw in db.execute("SELECT id,payload FROM mail_plans WHERE state IN ('running','review')"):
                previous = json.loads(outbox._dpapi(raw, decrypt=True))
                if previous['account'] != payload['account']:
                    continue
                occupied = {key(i['before']) for i in previous['items']}
                occupied.update(key(i['after']) for i in previous['items'] if i.get('after'))
                if old_id != identifier and wanted & occupied:
                    raise MailError('所选邮件有未核清的操作，请先查询原操作记录，不能重新移动。')
            return db.execute("UPDATE mail_plans SET state='running' WHERE id=? AND state='ready'", (identifier,)).rowcount == 1

    def attach_undo(self, identifier, candidate):
        with self.database() as db:
            self._table(db)
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM mail_plans WHERE id=?', (identifier,)).fetchone()
            original = json.loads(outbox._dpapi(row[0], decrypt=True))
            existing = original.get('undo_plan_id')
            if existing:
                db.execute("UPDATE mail_plans SET state='conflict' WHERE id=? AND state='ready'", (candidate,))
                return existing
            original['undo_plan_id'] = candidate
            db.execute('UPDATE mail_plans SET payload=? WHERE id=?', (self.encode(original), identifier))
            return candidate


def receipt(identifier, state, payload):
    return {'plan_id': identifier, 'plan_sha256': payload['digest'], 'status': state,
            'error': payload.get('error'),
            'items': [{k: v for k, v in item.items() if k not in {'before', 'after'}}
                      | {'source': reference(item['before']), 'destination': reference(item['after']) if item.get('after') else None}
                      for item in payload['items']],
            'next_action': 'execute_only_with_user_authorization' if state == 'ready' else 'inspect_results_do_not_repeat',
            'content_is_untrusted': True}


def load(client, identifier, store):
    state, payload = store.get_plan(identifier)
    if payload['account'] != client.config.address:
        raise MailError('当前邮箱与操作记录不一致。')
    return state, payload


def prepare(client, requests: list[dict], *, store=None):
    store = store or Plans()
    snapshots = batch(client, requests)
    with client.session() as connection:
        listed = folders(connection)
        selectable = {f['name'] for f in listed if f['selectable']}
        items = []
        for request, (raw, before) in zip(requests, snapshots):
            kind = request.get('action')
            if kind not in {'mark_read', 'mark_unread', 'move', 'archive'}:
                raise MailError('操作必须为 mark_read、mark_unread、move 或 archive。')
            if '\\deleted' in before['flags']:
                raise MailError('邮件已标记删除，停止整理，避免改变其他客户端的操作。')
            target = request.get('target', '')
            if kind == 'archive':
                target = archive_target(listed, request.get('categories'))
            target_validity = None
            if kind in {'archive', 'move'}:
                if target not in selectable:
                    raise MailError('目标文件夹不存在或不可选，请先按用户意愿创建并核验。')
                if target == before['mailbox']:
                    raise MailError('原位置与目标相同，无需移动。')
                target_validity, _ = client.select(connection, target)
            items.append({'before': before, 'action': kind, 'target': target,
                          'target_validity': target_validity, 'is_archive': is_archive(target, listed),
                          'summary': brief(raw), 'status': 'pending'})
    payload = {'account': client.config.address, 'created_at': outbox.utc_now(), 'items': items}
    identifier = store.create_plan(payload)
    return receipt(identifier, 'ready', payload)


def _matches(actual, expected):
    return actual['sha256'] == expected['sha256'] and actual['flags'] == expected['flags']


def _present(client, connection, ref):
    client.select(connection, ref['mailbox'], expected=ref['uid_validity'])
    data = _ok(*connection.uid('SEARCH', 'UID', str(ref['uid'])), '核对邮件是否存在')
    return str(ref['uid']).encode() in (data[0] or b'').split()


def execute(client, identifier, digest, *, store=None):
    store = store or Plans()
    state, payload = load(client, identifier, store)
    if digest != payload['digest']:
        raise MailError('计划摘要不一致，请重新检查原计划。')
    if state != 'ready' or not store.claim_plan(identifier, payload):
        state, payload = load(client, identifier, store)
        return receipt(identifier, state, payload)
    try:
        with client.session() as connection:
            capabilities = b' '.join(_ok(*connection.capability(), '查询服务能力')).upper().split()
            for item in payload['items']:
                before = item['before']
                _, current = snapshot(client, connection, before)
                if not _matches(current, before):
                    item['status'] = 'conflict'
                    continue
                if item['action'] in {'move', 'archive'}:
                    if b'UIDPLUS' not in capabilities:
                        raise MailError('服务器不支持安全的精确 UID 移动，已停止。')
                    client.select(connection, item['target'], expected=item['target_validity'])
                    _, current = snapshot(client, connection, before, readonly=False)
                    if not _matches(current, before):
                        item['status'] = 'conflict'
                        continue
                    item['status'] = 'copy_attempted'
                    store.save_plan(identifier, 'running', payload)
                    _ok(*connection.uid('COPY', str(before['uid']), quote(encode_mailbox(item['target']))), '复制指定邮件')
                    _, data = connection.response('COPYUID')
                    mapping = re.fullmatch(rb'(\d+) (\d+) (\d+)', (data or [b''])[0] or b'')
                    if not mapping or int(mapping[2]) != before['uid'] or int(mapping[1]) != item['target_validity']:
                        raise MailError('复制结果无法可靠对应，保留原邮件，请核查副本。')
                    after = {**before, 'mailbox': item['target'], 'uid_validity': int(mapping[1]), 'uid': int(mapping[3])}
                    item['after'] = after
                    item['status'] = 'copied'
                    store.save_plan(identifier, 'running', payload)
                    _, copied = snapshot(client, connection, after)
                    if not _matches(copied, after):
                        raise MailError('目标副本核验未通过，保留原邮件。')
                    _, current = snapshot(client, connection, before, readonly=False)
                    if not _matches(current, before):
                        raise MailError('复制后原邮件状态变化，保留两份供核查。')
                    item['status'] = 'remove_attempted'
                    store.save_plan(identifier, 'running', payload)
                    _ok(*connection.uid('STORE', str(before['uid']), '+FLAGS.SILENT', '(\\Deleted)'), '标记指定原邮件')
                    # UID EXPUNGE only this copied UID; never connection.expunge()/close().
                    _ok(*connection.uid('EXPUNGE', str(before['uid'])), '移除已核验的原邮件')
                    if _present(client, connection, before):
                        raise MailError('原邮件移除尚未核实，请检查操作记录。')
                    _, copied = snapshot(client, connection, after)
                    if not _matches(copied, after):
                        raise MailError('移动后目标状态发生变化，请核查。')
                else:
                    _, current = snapshot(client, connection, before, readonly=False)
                    if not _matches(current, before):
                        item['status'] = 'conflict'
                        continue
                    flags = set(before['flags'])
                    read = item['action'] == 'mark_read'
                    flags.add('\\seen') if read else flags.discard('\\seen')
                    item['after'] = {**before, 'flags': sorted(flags)}
                    item['status'] = 'flag_attempted'
                    store.save_plan(identifier, 'running', payload)
                    _ok(*connection.uid('STORE', str(before['uid']), '+FLAGS.SILENT' if read else '-FLAGS.SILENT', '(\\Seen)'), '修改已读状态')
                    _, current = snapshot(client, connection, before)
                    if not _matches(current, item['after']):
                        raise MailError('已读状态修改结果尚未核实。')
                item['status'] = 'completed'
                store.save_plan(identifier, 'running', payload)
        state = 'completed' if all(i['status'] == 'completed' for i in payload['items']) else 'partial'
    except MailError as exc:
        payload['error'] = str(exc)
        for item in payload['items']:
            if item['status'] == 'pending':
                item['status'] = 'not_attempted'
        state = 'review'
    store.save_plan(identifier, state, payload)
    result = receipt(identifier, state, payload)
    if payload.get('error'):
        result['error'] = payload['error']
    return result


def status(client, identifier, reconcile: bool = False, *, store=None):
    store = store or Plans()
    state, payload = load(client, identifier, store)
    result = receipt(identifier, state, payload)
    if reconcile:
        # Observation only. Never race with a live executor or reopen its durable gate.
        observations = []
        with client.session() as connection:
            for item in payload['items']:
                found = {}
                for name in ('before', 'after'):
                    ref = item.get(name)
                    if ref:
                        try:
                            present = _present(client, connection, ref)
                            found[name] = {'present': present}
                            if present:
                                _, current = snapshot(client, connection, ref)
                                found[name]['matches_snapshot'] = _matches(current, ref)
                        except MailError:
                            found[name] = {'status': 'unavailable'}
                observations.append(found)
        result['observations'] = observations
        result['reconciliation_changes_state'] = False
    return result


def prepare_undo(client, identifier, *, store=None):
    store = store or Plans()
    state, original = load(client, identifier, store)
    if state not in {'completed', 'partial'}:
        raise MailError('仅已核验完成的计划可准备撤销；结果不明必须先人工核查。')
    if original.get('undo_plan_id'):
        return status(client, original['undo_plan_id'], store=store)
    requests, expected, target_validities = [], [], []
    for item in original['items']:
        if item['status'] != 'completed':
            continue
        before, after = item['before'], item['after']
        if before['mailbox'] == after['mailbox']:
            request = {**reference(after), 'action': 'mark_read' if '\\seen' in before['flags'] else 'mark_unread'}
        else:
            request = {**reference(after), 'action': 'move', 'target': before['mailbox']}
        requests.append(request)
        expected.append(after)
        target_validities.append(before['uid_validity'] if before['mailbox'] != after['mailbox'] else None)
    if not requests:
        raise MailError('没有可撤销的已完成项目。')
    for (_, now), after in zip(batch(client, requests), expected):
        if not _matches(now, after):
            raise MailError('邮件在操作后已被改动，不能覆盖后续变化。')
    prepared = prepare(client, requests, store=store)
    _, undo = load(client, prepared['plan_id'], store)
    # Compare again against original result, not merely a newly captured baseline.
    if any(not _matches(i['before'], e) or i['target_validity'] != v
           for i, e, v in zip(undo['items'], expected, target_validities)):
        store.save_plan(prepared['plan_id'], 'conflict', undo)
        raise MailError('准备撤销时邮件发生变化，已禁用该撤销计划。')
    linked = store.attach_undo(identifier, prepared['plan_id'])
    return prepared if linked == prepared['plan_id'] else status(client, linked, store=store)
