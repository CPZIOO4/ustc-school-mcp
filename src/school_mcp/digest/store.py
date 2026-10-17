"""Encrypted per-profile subscriptions and reports; serialized cross-process cycles."""
from contextlib import contextmanager
import json
import os
import sqlite3
from pathlib import Path
from ..mail.config import local_dir
from ..mail.credentials import _dpapi
from . import native

def owner():
    from ..agent.store import identity
    return identity('digest') or 'public'

def defaults():
    return dict(enabled=False,sources=['teach'],keywords=[],exclude=[],locations=[],
        max_pages=3,detail_limit=10,interval_minutes=60,daily_time='08:00',urgent_hours=24,
        email='',email_enabled=False,bb_courses=[])

class Store:
    def __init__(self,root=None):
        # The shared legacy local_dir() resolves links. Preserve the explicit
        # profile spelling until the native boundary has checked its ancestors.
        raw=os.environ.get('SCHOOL_MCP_LOCAL_DIR')
        self.root=root or (Path(raw).expanduser().absolute() if native.enabled() and raw else local_dir())
        if native.enabled():native.check_paths(self.root)
        else:self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'digest.sqlite3'

    @contextmanager
    def cycle(self):
        if native.enabled():native.check_paths(self.root)
        path=self.root/'digest-cycle.lock'
        with path.open('a+b') as f:
            f.write(b'0');f.flush();f.seek(0)
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:
                raise ValueError('信息中心正在采集或发送，请稍后再试。') from None
            try:yield
            finally:
                f.seek(0)
                if os.name=='nt':msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(f.fileno(),fcntl.LOCK_UN)

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload BLOB NOT NULL)')
            db.execute('BEGIN IMMEDIATE');yield db;db.commit()
        except BaseException:
            db.rollback();raise
        finally:db.close()

    def read(self):
        if native.enabled():
            return native.call('read', root=native.directory(self.root), owner=owner())
        with self.db() as db:
            row=db.execute('SELECT payload FROM state WHERE id=1').fetchone()
        if not row:
            return dict(owner=owner(),settings=defaults(),items={},sources={},messages=[],pending={},last_daily=None,last_cycle=None)
        if len(row[0])>24*1024*1024:raise ValueError('信息中心记录超过上限。')
        result=json.loads(_dpapi(row[0],decrypt=True))
        if result['owner']!=owner():raise ValueError('订阅属于其他账号，请恢复原账号后管理。')
        return result

    def save(self,state):
        if native.enabled():
            native.call('save', root=native.directory(self.root), owner=owner(), state=state)
            return
        if state['owner']!=owner():raise ValueError('当前身份已变化，停止保存订阅。')
        raw=json.dumps(state,ensure_ascii=False,allow_nan=False).encode()
        if len(raw)>20*1024*1024:raise ValueError('信息中心记录超过上限，请缩小来源范围。')
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO state VALUES(1,?)',(_dpapi(raw),))
