from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from ..mail.credentials import _dpapi
from . import native_core


def now():
    return datetime.now(timezone.utc).isoformat()


def write_private(path, value):
    if native_core.enabled():
        native_core.call('private_write', {'path': str(Path(path).resolve()), 'value': value})
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = _dpapi(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_bytes(encrypted)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def read_private(path, default=None):
    if native_core.enabled():
        return native_core.call('private_read', {'path': str(Path(path).resolve()), 'default': default})
    path = Path(path)
    if not path.exists():
        return default
    if path.stat().st_size > 12 * 1024 * 1024:
        raise ValueError('本机记录过大，无法读取。')
    try:
        return json.loads(_dpapi(path.read_bytes(), decrypt=True))
    except Exception:
        raise ValueError('本机加密记录无法读取；请使用原 Windows 账户。') from None


class Data:
    def __init__(self, root=None):
        self.root = Path(root or Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'USTCSchoolAgent').resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / 'school-desktop.json'
        if not marker.exists():
            if any(p.name != 'diagnostics' for p in self.root.iterdir()):
                raise ValueError('应用数据目录须为空或是本应用创建的目录。')
            marker.write_text('{"application":"school-mcp-desktop","version":1}', encoding='utf-8')
        if json.loads(marker.read_text(encoding='utf-8')).get('application') != 'school-mcp-desktop':
            raise ValueError('不是学校应用数据目录。')
        self.profile = self.root / 'profile'
        self.profile.mkdir(exist_ok=True)
        from ..safety_log import configure_desktop
        configure_desktop(self.root)
        os.environ['SCHOOL_MCP_LOCAL_DIR'] = str(self.profile)
        os.environ['SCHOOL_MCP_BROWSER_HEADED'] = '0'
        os.environ.pop('SCHOOL_MAIL_PASSWORD', None)
        self.lock = threading.RLock()

    @property
    def settings_path(self):
        return self.root / 'model-settings.dpapi'

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.profile / 'desktop-chats.sqlite3', timeout=10)
            try:
                db.execute('CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, payload BLOB NOT NULL)')
                yield db
                db.commit()
            finally:
                db.close()

    def new_chat(self):
        value = dict(id=uuid.uuid4().hex, title='新的对话', created_at=now(), updated_at=now(),
                     messages=[], events=[], tasks=[], allowed_files=[], pending=None, state='idle')
        self.save_chat(value)
        return value

    def save_chat(self, chat):
        if native_core.enabled():
            with self.lock:
                result = native_core.call('chat_save', {'database': str(self.profile / 'desktop-chats.sqlite3'), 'chat': chat})
                chat['updated_at'] = result['updated_at']
            return
        raw = json.dumps(chat, ensure_ascii=False, allow_nan=False).encode()
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError('对话已达到本机记录上限，请建立新的对话。')
        chat['updated_at'] = now()
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO chats VALUES (?,?)',
                       (chat['id'], _dpapi(json.dumps(chat, ensure_ascii=False).encode())))

    def chat(self, identifier):
        if native_core.enabled():
            with self.lock:
                return native_core.call('chat_read', {'database': str(self.profile / 'desktop-chats.sqlite3'), 'id': identifier})
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
            raise ValueError('无效的对话编号。')
        with self.db() as db:
            row = db.execute('SELECT payload FROM chats WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('对话不存在。')
        return json.loads(_dpapi(row[0], decrypt=True))

    def chats(self):
        if native_core.enabled():
            with self.lock:
                return native_core.call('chat_list', {'database': str(self.profile / 'desktop-chats.sqlite3')})
        with self.db() as db:
            rows = db.execute('SELECT payload FROM chats ORDER BY rowid DESC LIMIT 50').fetchall()
        values = [json.loads(_dpapi(r[0], decrypt=True)) for r in rows]
        return [{k: c[k] for k in ('id', 'title', 'updated_at', 'state')} for c in values]

    def recover(self):
        # Crashed turns are never restarted automatically, including an API request.
        with self.db() as db:
            rows = db.execute('SELECT payload FROM chats').fetchall()
        for row in rows:
            chat = json.loads(_dpapi(row[0], decrypt=True))
            if chat['state'] == 'running':
                from ..safety_log import record
                incident = record(code='interrupted', stage='recovery', outcome='interrupted')
                chat['state'] = 'interrupted'
                chat['events'].append({'kind': 'notice', 'text': '上次对话中断。先核实关联任务，不能重复提交。', 'detail': incident})
                self.save_chat(chat)
