"""On-demand approved rules, frozen previews, and guarded archive plans."""
from __future__ import annotations

import hashlib
import json
import uuid
from email.utils import getaddresses

from . import actions, outbox
from .config import MailError
from .parsing import parse_email
from .selection import batch, brief, key, reference


class ClassificationStore(outbox.Outbox):
    def _table(self, db):
        db.execute('CREATE TABLE IF NOT EXISTS mail_classification (id TEXT PRIMARY KEY, payload BLOB NOT NULL)')

    def put(self, identifier, payload):
        encrypted = self.encode(payload)
        with self.database() as db:
            self._table(db)
            db.execute('INSERT OR REPLACE INTO mail_classification VALUES (?,?)', (identifier, encrypted))

    def get(self, identifier):
        with self.database() as db:
            self._table(db)
            row = db.execute('SELECT payload FROM mail_classification WHERE id=?', (identifier,)).fetchone()
        return json.loads(outbox._dpapi(row[0], decrypt=True)) if row else None


def _config_id(client):
    return 'config-' + hashlib.sha256(client.config.address.encode()).hexdigest()


def get_rules(client, *, store=None):
    saved = (store or ClassificationStore()).get(_config_id(client))
    return saved or {'categories': [], 'rules': [], 'configured': False, 'mode': 'on_demand'}


def save_rules(client, categories: list[str], rules: list[dict], *, store=None):
    if (not categories or len(categories) > 20 or len(set(categories)) != len(categories)
            or len(rules) > 50):
        raise MailError('需要 1–20 个不重复的已确认类别，最多 50 条规则。')
    listed = client.folders()['folders']
    for category in categories:
        actions.archive_target(listed, [category])
    normalized = []
    for rule in rules:
        category = rule.get('category')
        field, value = rule.get('field'), rule.get('value')
        priority = rule.get('priority', 100)
        if (category not in categories or field not in {'sender', 'domain', 'subject_contains'}
                or not isinstance(value, str) or not value.strip() or len(value) > 200
                or type(priority) is not int or not 0 <= priority <= 1000):
            raise MailError('规则需指定已确认类别、sender/domain/subject_contains、匹配值和 0–1000 优先级。')
        if field == 'sender' and not outbox.valid_address(value):
            raise MailError('发件人规则必须为完整邮箱地址。')
        if field == 'domain' and not outbox.valid_address('test@' + value):
            raise MailError('域名规则必须为纯域名，精确匹配，不自动包含子域名。')
        normalized.append({'category': category, 'field': field, 'value': value.strip().casefold(), 'priority': priority})
    payload = {'configured': True, 'mode': 'on_demand', 'categories': categories,
               'rules': normalized, 'revision': uuid.uuid4().hex, 'protect_authentication': True}
    (store or ClassificationStore()).put(_config_id(client), payload)
    return payload


def _decision(raw, config):
    parsed = parse_email(raw, max_chars=len(raw) + 1)
    text = (parsed['subject'] + '\n' + parsed['body']).casefold()
    # Deliberately conservative. Read status is never a classifier.
    protected = ('验证码', '校验码', '动态口令', '重置密码', '密码重置', 'password reset',
                 'reset your password', 'verification code', 'one-time', 'authentication code', 'security code')
    if any(term in text for term in protected):
        return 'protected', None
    senders = {a.casefold() for _, a in getaddresses([parsed['from']])}
    domains = {a.rsplit('@', 1)[1] for a in senders if '@' in a}
    matches = []
    for rule in config['rules']:
        field, value = rule['field'], rule['value']
        if ((field == 'sender' and value in senders) or (field == 'domain' and value in domains)
                or (field == 'subject_contains' and value in parsed['subject'].casefold())):
            matches.append(rule)
    if not matches:
        return 'unmatched', None
    priority = min(r['priority'] for r in matches)
    winners = {r['category'] for r in matches if r['priority'] == priority}
    return ('rule', next(iter(winners))) if len(winners) == 1 else ('conflict', None)


def preview(client, messages: list[dict], max_chars: int = 500, *, store=None):
    if not 0 <= max_chars <= 6000:
        raise MailError('分类预览正文上限为 0–6000 字符。')
    store = store or ClassificationStore()
    config = get_rules(client, store=store)
    if not config['configured']:
        return {'status': 'needs_categories', 'next_action': 'agree_categories_then_save_rules'}
    items, public = [], []
    for raw, snap in batch(client, messages):
        decision, category = _decision(raw, config)
        item = {'snapshot': snap, 'decision': decision, 'category': category}
        items.append(item)
        parsed = parse_email(raw, max_chars=max(1, max_chars))
        public.append({**reference(snap), **brief(raw), 'decision': decision, 'category': category,
                       'body_excerpt': parsed['body'][:max_chars] if decision == 'unmatched' else '',
                       'body_truncated': parsed['body_truncated'] if decision == 'unmatched' else False})
    identifier = uuid.uuid4().hex
    payload = {'account': client.config.address, 'config': config, 'items': items}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    payload['digest'] = digest
    store.put(identifier, payload)
    return {'status': 'preview', 'preview_id': identifier, 'preview_sha256': digest, 'items': public,
            'categories': config['categories'], 'content_is_untrusted': True,
            'next_action': 'suggest_only_unmatched_then_prepare_classification'}


def prepare(client, identifier: str, digest: str, suggestions: list[dict] | None = None, *, store=None, plans=None):
    store = store or ClassificationStore()
    saved = store.get(identifier)
    if not saved or saved.get('account') != client.config.address or saved.get('digest') != digest:
        raise MailError('分类预览不存在、账号或摘要不匹配。')
    if get_rules(client, store=store).get('revision') != saved['config']['revision']:
        raise MailError('分类规则已经改变，请重新预览。')
    by_key = {}
    for suggestion in suggestions or []:
        ref = reference(suggestion)
        if key(ref) in by_key or suggestion.get('category') not in saved['config']['categories'] or not suggestion.get('reason', '').strip():
            raise MailError('建议需使用已有类别、给出依据，且不得重复。')
        by_key[key(ref)] = suggestion
    requests, expected = [], []
    for item in saved['items']:
        snap = item['snapshot']
        suggestion = by_key.pop(key(snap), None)
        if suggestion and item['decision'] != 'unmatched':
            raise MailError('模型建议只能处理未匹配邮件，不能覆盖保护或冲突规则。')
        category = item['category'] if item['decision'] == 'rule' else (suggestion or {}).get('category')
        if category:
            requests.append({**reference(snap), 'action': 'archive', 'categories': [category]})
            expected.append(snap)
    if by_key:
        raise MailError('建议包含预览范围之外的邮件。')
    if not requests:
        return {'status': 'no_changes', 'next_action': 'keep_original_locations'}
    plans = plans or actions.Plans()
    result = actions.prepare(client, requests, store=plans)
    _, payload = actions.load(client, result['plan_id'], plans)
    if (get_rules(client, store=store).get('revision') != saved['config']['revision']
            or any(not actions._matches(i['before'], e) for i, e in zip(payload['items'], expected))):
        plans.save_plan(result['plan_id'], 'conflict', payload)
        raise MailError('预览后邮件或规则发生变化，已禁用该分类计划，请重新预览。')
    return result
