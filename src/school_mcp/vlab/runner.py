"""Copied to a dedicated VM job directory, then run once by the authorized adapter."""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time


def main():
    task = json.loads(Path('task.json').read_text(encoding='utf-8'))
    if Path('started.json').exists():
        return
    with Path('started.json').open('x', encoding='utf-8') as f:
        json.dump({'started_at': time.time()}, f)
    result = {'completed': False, 'exit_code': None, 'stdout': '', 'stderr': '', 'timed_out': False, 'output_truncated': False}
    try:
        child = subprocess.Popen(task['command'] or 'true', shell=True, executable='/bin/bash',
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        selected = selectors.DefaultSelector()
        selected.register(child.stdout, selectors.EVENT_READ, 'stdout')
        selected.register(child.stderr, selectors.EVENT_READ, 'stderr')
        buffers = {'stdout': bytearray(), 'stderr': bytearray()}
        deadline = time.monotonic() + task['timeout']
        while selected.get_map():
            if time.monotonic() >= deadline:
                result['timed_out'] = True
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # Stop even when a detached descendant inherited a pipe.
                break
            for key, _ in selected.select(.25):
                data = os.read(key.fileobj.fileno(), 8192)
                if not data:
                    selected.unregister(key.fileobj)
                    continue
                buf = buffers[key.data]
                remaining = max(0, 32768 - len(buf))
                result['output_truncated'] |= len(data) > remaining
                buf.extend(data[:remaining])
        selected.close()
        result['exit_code'] = child.wait(timeout=5)
        result.update({k: bytes(v).decode('utf-8', 'replace') for k,v in buffers.items()})
        result['completed'] = True
    except Exception as exc:
        result['error_type'] = type(exc).__name__
    result['finished_at'] = time.time()
    Path('result.tmp').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    Path('result.tmp').replace('result.json')


if __name__ == '__main__':
    main()
