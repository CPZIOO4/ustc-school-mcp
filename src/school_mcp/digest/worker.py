"""One hidden fixed worker per private profile; survives closing the browser UI."""
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import time
from .store import Store
from ..safety_log import record

@contextmanager
def worker_lock(store):
    with (store.root/'digest-worker.lock').open('a+b') as f:
        f.write(b'0');f.flush();f.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            yield False;return
        try:yield True
        finally:
            f.seek(0)
            if os.name=='nt':msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(f.fileno(),fcntl.LOCK_UN)

def running(store=None):
    with worker_lock(store or Store()) as obtained:
        return not obtained

def ensure(*, allow_in_process=False):
    store=Store()
    if not store.read()['settings']['enabled']:return {'running':running(store),'enabled':False}
    if running(store):return {'running':True,'enabled':True}
    from ..background_process import spawn
    executable=Path(sys.executable)
    if not getattr(sys,'frozen',False) and os.name=='nt':executable=executable.with_name('pythonw.exe')
    env=os.environ.copy();env['SCHOOL_MCP_LOCAL_DIR']=str(store.root.resolve());env['SCHOOL_MCP_BROWSER_HEADED']='0'
    mode='independent_process'
    try:
        p=spawn([str(executable),'-m','school_mcp','digest-worker','--private-dir',str(store.root.resolve())],
                cwd=str(Path(__file__).resolve().parents[3]),env=env)
    except PermissionError:
        if not allow_in_process:raise
        # Some Windows host jobs deny breakaway. Only the long-lived desktop
        # server may host this worker; a short CLI/MCP caller must fail visibly.
        import threading
        p=None;mode='application_process'
        threading.Thread(target=run,args=(str(store.root.resolve()),),daemon=True,
                         name='campus-digest').start()
    for _ in range(30):
        if running(store):return {'running':True,'enabled':True,'mode':mode}
        if p is not None and p.poll() is not None:break
        time.sleep(.1)
    return {'running':False,'enabled':True,'state':'start_unverified','note':'后台进程尚未确认启动；查看状态，不假定已在监控。'}

def run(private_dir):
    os.environ['SCHOOL_MCP_LOCAL_DIR']=str(Path(private_dir).resolve())
    os.environ['SCHOOL_MCP_BROWSER_HEADED']='0'
    from .engine import refresh
    store=Store()
    with worker_lock(store) as obtained:
        if not obtained:return
        while True:
            try:
                if not store.read()['settings']['enabled']:return
                parent=store.root.parent
                if (parent/'school-desktop.json').exists() and (parent/'locked').exists():
                    time.sleep(5);continue
                refresh(notify=True,store=store)
            except Exception as exc:
                record(service='digest',stage='background',exc=exc,outcome='unknown')
            time.sleep(5)
