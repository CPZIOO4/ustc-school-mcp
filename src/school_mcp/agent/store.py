"""Encrypted coordinator context; never a replacement for business receipts."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import re
import sqlite3
import uuid

from ..mail.config import local_dir, load_config
from ..mail.credentials import _dpapi
from ..workflow_store import digest
from ..desktop import native_core


def now():
    return datetime.now(timezone.utc).isoformat()


def identity(service):
    try:
        if service in {'teach', 'web'}:
            return 'public'
        if service == 'mail':
            return digest(load_config().address.lower())
        if service in {'icourse', 'nan7'}:
            from ..task_recovery import account
            return account(service)
        from ..bb.identity import load_credentials
        return digest(load_credentials()['username'])
    except Exception:
        return None


def pack(value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('任务上下文过大；缩小任务，不保存原始网页或附件。')
    return _dpapi(raw)


class Store:
    def __init__(self):
        self.path = local_dir() / 'agent-tasks.sqlite3'

    def native(self, operation, **values):
        """Fail closed on pipe errors: a lost claim response must never be retried."""
        return native_core.call('agent_' + operation, dict(database=str(self.path.resolve()), **values))

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload BLOB NOT NULL)')
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create(self, workflow, goal, account):
        if native_core.enabled():
            return self.native('create', workflow=workflow, goal=goal, account=account)
        task = dict(task_id=uuid.uuid4().hex, workflow=workflow, goal=goal, account=account,
                    created_at=now(), updated_at=now(), state='active', calls=[], links=[])
        with self.db() as db:
            self.save(db, task)
        return task

    def load(self, db, identifier):
        if not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise ValueError('task_id 无效。')
        row = db.execute('SELECT payload FROM tasks WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('任务不存在。')
        return json.loads(_dpapi(row[0], decrypt=True))

    def save(self, db, task):
        task['updated_at'] = now()
        db.execute('INSERT OR REPLACE INTO tasks VALUES (?,?)', (task['task_id'], pack(task)))

    @contextmanager
    def reader(self):
        if not self.path.exists():
            raise ValueError('尚无智能体任务。')
        connection = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
        try:
            connection.execute('PRAGMA query_only=ON')
            yield connection
        finally:
            connection.close()

    def read(self, identifier):
        if native_core.enabled():
            return self.native('read', task_id=identifier)
        with self.reader() as db:
            return self.load(db, identifier)

    def list(self, offset, limit):
        if native_core.enabled():
            result = self.native('list', offset=offset, limit=limit)
            return result['tasks'], result['next_offset']
        if not self.path.exists():
            return [], None
        with self.reader() as db:
            rows = db.execute('SELECT payload FROM tasks ORDER BY rowid DESC LIMIT ? OFFSET ?',
                              (limit + 1, offset)).fetchall()
        return [json.loads(_dpapi(row[0], decrypt=True)) for row in rows[:limit]], offset + limit if len(rows) > limit else None
