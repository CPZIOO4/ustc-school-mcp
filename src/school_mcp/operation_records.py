"""Account-bound durable IO workflow records with cross-process claims."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import re
import sqlite3
import uuid
from .mail.config import local_dir
from .mail.credentials import _dpapi
from .bb.identity import load_credentials
from .service_errors import ServiceError


def stamp():
    return datetime.now(timezone.utc).isoformat()


class Records:
    def __init__(self, kind):
        if kind not in {'study', 'compute'}:
            raise ValueError('Unknown workflow')
        self.kind = kind
        self.account = load_credentials()['username'].casefold()
        self.path = local_dir() / 'io-workflows.sqlite3'

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT, account TEXT, payload BLOB)')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def load(self, db, identifier):
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
            raise ValueError('任务编号无效。')
        row = db.execute('SELECT payload FROM records WHERE id=? AND kind=? AND account=?', (identifier, self.kind, self.account)).fetchone()
        if not row:
            raise ValueError('任务不存在或属于另一个账号。')
        return json.loads(_dpapi(row[0], decrypt=True))

    def save(self, db, v):
        if load_credentials()['username'].casefold() != self.account:
            raise ServiceError('账号已变化，停止处理。', code='identity_mismatch')
        v['updated_at'] = stamp()
        raw = json.dumps(v, ensure_ascii=False, allow_nan=False).encode()
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError('任务记录过大。')
        db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?)', (v['id'], self.kind, self.account, _dpapi(raw)))

    def create(self, value):
        value = dict(value, id=uuid.uuid4().hex, created_at=stamp())
        with self.db() as db:
            self.save(db, value)
        return value

    def read(self, identifier):
        with self.db() as db:
            return self.load(db, identifier)

    def update(self, identifier, **fields):
        with self.db() as db:
            v = self.load(db, identifier)
            v.update(fields)
            self.save(db, v)
            return v

    def claim(self, identifier, field):
        with self.db() as db:
            v = self.load(db, identifier)
            if v.get(field):
                return None
            v[field] = stamp()
            self.save(db, v)
            return v
