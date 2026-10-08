"""Private frozen plans and durable, cross-process one-shot execution claims."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

from .mail.config import local_dir
from .mail.credentials import _dpapi


class WorkflowError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise WorkflowError(message)


class PlanStore:
    def __init__(self, service, account, directory=None):
        require(service in {'jw', 'nan7', 'icourse'}, '未知业务。')
        self.service, self.account = service, account
        self.path = (directory or local_dir()) / 'business-plans.sqlite3'

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, service TEXT, account TEXT, target TEXT, digest TEXT, state TEXT, phase TEXT, payload BLOB, result BLOB)')
            db.execute('CREATE TABLE IF NOT EXISTS locks (target TEXT PRIMARY KEY, plan TEXT)')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except sqlite3.Error:
            db.rollback()
            raise WorkflowError('本机业务记录不可用；停止执行，不能重发。') from None
        finally:
            db.close()

    def create(self, target, content, snapshot, session, preview):
        sha = digest({'content': content, 'snapshot': snapshot, 'session': session})
        key = digest([self.service, self.account, target])
        plan = dict(content=content, snapshot=snapshot, session=session, preview=preview,
                    expires_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat())
        with self.db() as db:
            row = db.execute("SELECT id FROM plans WHERE service=? AND account=? AND target=? AND digest=? AND state NOT IN ('stopped', 'expired')", (self.service, self.account, key, sha)).fetchone()
            if row:
                existing=self.view(row[0], db=db)
                if existing['state']!='expired':
                    return existing
                db.execute("UPDATE plans SET state='expired' WHERE id=?",(row[0],))
            identifier = uuid.uuid4().hex
            db.execute('INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,NULL)', (identifier, self.service, self.account, key, sha, 'ready', 'prepared', _dpapi(json.dumps(plan, ensure_ascii=False).encode())))
            return self.view(identifier, db=db)

    def _row(self, identifier, db):
        require(isinstance(identifier, str) and re.fullmatch(r'[a-f0-9]{32}', identifier), 'plan_id 无效。')
        row = db.execute('SELECT digest,state,phase,payload,result,target FROM plans WHERE id=? AND service=? AND account=?', (identifier, self.service, self.account)).fetchone()
        require(row is not None, '业务记录不存在或不属于当前账号。')
        return row

    def get(self, identifier):
        with self.db() as db:
            row = self._row(identifier, db)
        return dict(sha256=row[0], state=row[1], phase=row[2], plan=json.loads(_dpapi(row[3], decrypt=True)), result=json.loads(_dpapi(row[4], decrypt=True)) if row[4] else None)

    def view(self, identifier, db=None):
        if db is None:
            with self.db() as connection:
                return self.view(identifier, db=connection)
        row = self._row(identifier, db)
        plan = json.loads(_dpapi(row[3], decrypt=True))
        expired = datetime.fromisoformat(plan['expires_at']) <= datetime.now(timezone.utc)
        state = 'expired' if row[1] == 'ready' and expired else row[1]
        result = json.loads(_dpapi(row[4], decrypt=True)) if row[4] else None
        execute_tool={'nan7':'school_nan7_publish_offer','icourse':'school_icourse_publish_review','jw':None}[self.service]
        status_tool={'nan7':'school_nan7_offer_status','icourse':'school_icourse_review_status','jw':None}[self.service]
        verification_available=self.service=='icourse' or self.service=='nan7' and bool((result or {}).get('offer_id'))
        return dict(plan_id=identifier, content_sha256=row[0], state=state, phase=row[2],
                    preview=plan['preview'], result=result, can_execute=state == 'ready',
                    next_tool=execute_tool if state=='ready' else status_tool if state in {'executing','uncertain'} else None,
                    next_arguments={'plan_id':identifier,'content_sha256':row[0]} if state=='ready' else {'plan_id':identifier,'verify':verification_available} if state in {'executing','uncertain'} else {},
                    authorization_required=state=='ready', retry_write_allowed=False,
                    expires_at=plan['expires_at'], next_action={
                        'ready': 'review_preview_then_execute_if_authorized',
                        'executing': 'check_status_do_not_retry', 'uncertain': 'verify_only_do_not_retry',
                        'verified': 'done', 'stopped': 'review_reason', 'expired': 'prepare_again',
                    }[state], content_is_untrusted=True)

    def claim(self, identifier, sha):
        with self.db() as db:
            row = self._row(identifier, db)
            require(row[0] == sha, '材料摘要不匹配；重新展示原准备记录。')
            if row[1] != 'ready':
                return False
            plan = json.loads(_dpapi(row[3], decrypt=True))
            if datetime.fromisoformat(plan['expires_at']) <= datetime.now(timezone.utc):
                db.execute("UPDATE plans SET state='expired' WHERE id=?", (identifier,))
                return False
            require(not db.execute('SELECT plan FROM locks WHERE target=?', (row[5],)).fetchone(), '此业务已有执行中或结果不明的记录；先核实原记录。')
            db.execute('INSERT INTO locks VALUES (?,?)', (row[5], identifier))
            db.execute("UPDATE plans SET state='executing',phase='preflight' WHERE id=?", (identifier,))
        return True

    def phase(self, identifier, phase):
        with self.db() as db:
            row = self._row(identifier, db)
            require(row[1] == 'executing', '执行状态已变化。')
            # A repeated phase would replay a side effect, including image uploads.
            require(row[2] != phase, '此步骤已请求，不能重复。')
            db.execute('UPDATE plans SET phase=? WHERE id=?', (phase, identifier))

    def finish(self, identifier, state, result):
        require(state in {'verified', 'uncertain', 'stopped'}, '无效结果状态。')
        with self.db() as db:
            row = self._row(identifier, db)
            if row[1] not in {'executing', 'uncertain'}:
                return
            db.execute('UPDATE plans SET state=?,result=? WHERE id=?', (state, _dpapi(json.dumps(result, ensure_ascii=False).encode()), identifier))
            if state in {'verified', 'stopped'}:
                db.execute('DELETE FROM locks WHERE plan=?', (identifier,))

    def checkpoint(self, identifier, result):
        """Persist the returned remote identity before starting readback."""
        with self.db() as db:
            row = self._row(identifier, db)
            require(row[1] == 'executing', '执行状态已变化。')
            db.execute('UPDATE plans SET result=? WHERE id=?', (_dpapi(json.dumps(result).encode()), identifier))
