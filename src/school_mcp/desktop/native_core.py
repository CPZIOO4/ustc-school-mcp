"""Temporary private-pipe adapter while school services migrate to Rust.

The native host supplies the packaged executable. There is no remote endpoint,
secret in argv, automatic retry, or silent Python fallback after native failure.
"""
import atexit
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import threading

MAX_FRAME = 16 * 1024 * 1024
class Worker:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.path = None

    def stop(self):
        process, self.process, self.path = self.process, None, None
        if process is not None:
            if process.poll() is None:
                # EOF lets owned browser managers terminate Chrome and remove temporary profiles.
                if process.stdin:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()


# Slow provider requests must not block local storage, status, or task recovery.
_workers = {name: Worker() for name in ('network', 'public', 'authenticated', 'browser', 'bb_io', 'young_io', 'login', 'local')}


def enabled():
    return bool(os.environ.get('SCHOOL_NATIVE_CORE'))


def shutdown():
    for worker in _workers.values():
        with worker.lock:
            worker.stop()


atexit.register(shutdown)


def _read_exact(stream, length):
    parts = bytearray()
    while len(parts) < length:
        chunk = stream.read(length - len(parts))
        if not chunk:
            raise EOFError()
        parts.extend(chunk)
    return bytes(parts)


def call(operation, value):
    from .model import ModelError
    raw = json.dumps({'operation': operation, 'input': value}, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(raw) > MAX_FRAME:
        raise ModelError('context_capacity', '本轮本机请求过大；原记录保留，请缩小任务范围。')
    channel = ('network' if operation in {'model_complete', 'model_list'} or (operation == 'app_io' and value.get('action') in {'list_models', 'test_connection'}) else
               operation if operation in {'bb_io', 'young_io'} else
               'public' if operation == 'public_read' else
               'authenticated' if operation == 'authenticated_read' else
               'login' if operation.startswith('login_') else
               'browser' if operation.startswith('browser_') else 'local')
    worker = _workers[channel]
    with worker.lock:
        try:
            path = Path(os.environ['SCHOOL_NATIVE_CORE'])
            if not path.is_absolute() or not path.is_file():
                raise ValueError()
            if worker.process is not None and (str(path) != worker.path or worker.process.poll() is not None):
                worker.stop()
            if worker.process is None:
                worker.process = subprocess.Popen([str(path), '--stdio'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                worker.path = str(path)
            process = worker.process
            replies = queue.Queue(maxsize=1)

            def exchange():
                try:
                    process.stdin.write(struct.pack('>I', len(raw)) + raw)
                    process.stdin.flush()
                    size = struct.unpack('>I', _read_exact(process.stdout, 4))[0]
                    if not 0 < size <= MAX_FRAME:
                        raise ValueError()
                    replies.put(json.loads(_read_exact(process.stdout, size)))
                except Exception:
                    replies.put(None)

            reader = threading.Thread(target=exchange, daemon=True)
            reader.start()
            result = replies.get(timeout=600 if operation == 'bb_io' else 135)
            if not isinstance(result, dict) or type(result.get('ok')) is not bool:
                raise ValueError()
            if result['ok']:
                return result['result']
            error = result['error']
            if not isinstance(error, dict) or not isinstance(error.get('code'), str) or not isinstance(error.get('message'), str):
                raise ValueError()
            raise ModelError(error['code'], error['message'])
        except ModelError:
            raise
        except Exception:
            worker.stop()
            raise ModelError('runtime_failure', 'Rust 核心通信中断；本轮已停止，没有自动重复请求。请重新打开应用后核实任务。') from None
