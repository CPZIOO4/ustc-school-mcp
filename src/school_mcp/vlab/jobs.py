"""Frozen authorized VM jobs, bounded file transfer, and durable no-replay receipts."""
import errno
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import time
import uuid
from ..bb.identity import load_credentials
from ..mail.config import local_dir
from ..workflow_store import PlanStore, digest
from ..service_errors import ServiceError
from .client import ssh, machines

MAX_FILE = 50 * 1024 * 1024


def store():
    return PlanStore('vlab', digest(load_credentials()['username']))


def prepare(vm_id, command='', files=None, username='vlab', timeout=120):
    if type(vm_id) is not int or not isinstance(command, str) or len(command) > 12000 or '\x00' in command or username not in {'vlab','ubuntu','root'} or not 1 <= timeout <= 600:
        raise ServiceError('虚拟机任务参数无效。', code='invalid_arguments')
    files = files or []
    if len(files) > 10 or not command and not files:
        raise ServiceError('请提供命令或需要上传的文件，最多 10 个。', code='invalid_arguments')
    manifests = []
    for value in files:
        path = Path(value).resolve()
        if not path.is_file() or path.stat().st_size > MAX_FILE or re.search(r'[\x00-\x1f/\\]', path.name):
            raise ServiceError('上传文件不存在或超过 50 MiB。', code='invalid_arguments')
        if path.name in {x['name'] for x in manifests}:
            raise ServiceError('上传文件有同名项，请先重命名。', code='invalid_arguments')
        manifests.append({'path': str(path), 'name': path.name, 'size': path.stat().st_size,
                          'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    if sum(x['size'] for x in manifests) > MAX_FILE:
        raise ServiceError('本次上传总大小超过 50 MiB。', code='invalid_arguments')
    if vm_id not in {x['vm_id'] for x in machines()['machines']}:
        raise ServiceError('虚拟机不属于当前账号。', code='invalid_arguments')
    content = {'vm_id': vm_id, 'username': username, 'command': command, 'timeout': timeout, 'files': manifests}
    preview = {'vm_id': vm_id, 'username': username, 'command': command or '仅上传材料', 'timeout_seconds': timeout,
               'files': [{'name':x['name'], 'size_bytes':x['size'], 'remote_relative_path':'inputs/' + x['name']} for x in manifests],
               'note': '在该虚拟机用户下执行；工作目录是独立任务文件夹，不是命令沙箱。不会自动重试命令。'}
    return store().create(target=digest(content), content=content, snapshot={}, session={}, preview=preview)


def execute(plan_id, content_sha256, authorized=False):
    if authorized is not True:
        raise ServiceError('需要用户对该虚拟机、命令和材料的授权。', code='authorization_required')
    saved = store(); item = saved.get(plan_id); content = item['plan']['content']
    # Recheck material hashes before claiming any remote side effect.
    for f in content['files']:
        path = Path(f['path'])
        if not path.is_file() or path.stat().st_size != f['size'] or hashlib.sha256(path.read_bytes()).hexdigest() != f['sha256']:
            raise ServiceError('准备后的材料发生变化，请重新准备。', code='invalid_arguments')
    if not saved.claim(plan_id, content_sha256):
        return saved.view(plan_id)
    try:
        with ssh(content['vm_id'], content['username']) as client, client.open_sftp() as sftp:
            home = sftp.normalize('.')
            parent = home.rstrip('/') + '/school-agent-jobs'
            try:
                sftp.mkdir(parent, mode=0o700)
            except OSError:
                sftp.stat(parent)
            # Never use a pre-existing job directory; mkdir is an exclusive step.
            folder = parent + '/' + plan_id
            sftp.mkdir(folder, mode=0o700)
            sftp.mkdir(folder + '/inputs', mode=0o700)
            saved.checkpoint(plan_id, {'remote_directory': folder, 'vm_id': content['vm_id']})
            for f in content['files']:
                raw = Path(f['path']).read_bytes()
                if hashlib.sha256(raw).hexdigest() != f['sha256']:
                    raise ServiceError('材料在上传前发生变化。', code='invalid_arguments')
                with sftp.file(folder + '/inputs/' + f['name'], 'wb') as out:
                    out.write(raw)
            with sftp.file(folder + '/runner.py', 'w') as out:
                out.write(Path(__file__).with_name('runner.py').read_bytes())
            with sftp.file(folder + '/task.json', 'w') as out:
                out.write(json.dumps({'command':content['command'], 'timeout':content['timeout']}, ensure_ascii=False).encode('utf-8'))
            saved.phase(plan_id, 'starting_remote_job')
            _, stdout, stderr = client.exec_command('cd ' + shlex.quote(folder) + ' && nohup python3 runner.py >/dev/null 2>&1 </dev/null &', timeout=20)
            stdout.channel.recv_exit_status()
        return saved.view(plan_id)
    except Exception:
        previous = saved.get(plan_id).get('result') or {}
        saved.finish(plan_id, 'uncertain', {**previous, 'note': '远程准备或启动结果未完全确认。只查原任务状态，不能重发。'})
        return saved.view(plan_id)


def record(plan_id):
    item = store().get(plan_id)
    content, result = item['plan']['content'], item.get('result') or {}
    if not result.get('remote_directory'):
        raise ServiceError('该任务还没有可核实的远程目录。', code='unavailable')
    return item, content, result


def status(plan_id, verify=True, wait_seconds=0):
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 20:
        raise ServiceError('等待时间须为 0–20 秒。', code='invalid_arguments')
    saved = store(); item = saved.get(plan_id)
    if not verify or item['state'] not in {'executing', 'uncertain'}:
        return saved.view(plan_id)
    item, content, result = record(plan_id)
    with ssh(content['vm_id'], content['username']) as client, client.open_sftp() as sftp:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                with sftp.file(result['remote_directory'] + '/result.json', 'rb') as f:
                    raw = f.read(100001)
                if len(raw) > 100000:
                    raise ValueError('oversized receipt')
                receipt = json.loads(raw)
                if type(receipt.get('completed')) is not bool or receipt.get('exit_code') is not None and type(receipt['exit_code']) is not int:
                    raise ValueError('invalid receipt')
                break
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {**saved.view(plan_id), 'remote_result_available': False,
                            'note': '尚无结束回执，可能仍在运行或启动中断；保留原任务，稍后查询，不重发命令。'}
                time.sleep(min(2, remaining))
    saved.finish(plan_id, 'verified' if receipt['completed'] else 'stopped', {**result, **receipt, 'untrusted_content': True,
                 'succeeded': receipt['completed'] and receipt.get('exit_code') == 0 and not receipt.get('timed_out')})
    return saved.view(plan_id)


def remote_path(sftp, folder, relative):
    if not isinstance(relative, str):
        raise ServiceError('远程路径必须是文本。', code='invalid_arguments')
    p = PurePosixPath(relative)
    if p.is_absolute() or '..' in p.parts or '\\' in relative or '\x00' in relative or len(relative)>500:
        raise ServiceError('仅允许当前任务目录中的相对路径。', code='invalid_arguments')
    root = sftp.normalize(folder); target = sftp.normalize(root + '/' + relative)
    if target != root and not target.startswith(root.rstrip('/') + '/'):
        raise ServiceError('远程链接超出了当前任务目录。', code='invalid_arguments')
    return target


def files(plan_id, relative=''):
    _, content, result = record(plan_id)
    with ssh(content['vm_id'], content['username']) as client, client.open_sftp() as sftp:
        path = remote_path(sftp, result['remote_directory'], relative)
        import stat
        items = sftp.listdir_attr(path)
        return {'items': [{'name':x.filename, 'size_bytes':x.st_size, 'directory':stat.S_ISDIR(x.st_mode),
                           'path':str(PurePosixPath(relative) / x.filename)} for x in items[:100]], 'truncated':len(items)>100,
                'plan_id':plan_id, 'untrusted_content':True}


def download(plan_id, relative):
    _, content, result = record(plan_id)
    with ssh(content['vm_id'], content['username']) as client, client.open_sftp() as sftp:
        path = remote_path(sftp, result['remote_directory'], relative)
        if sftp.stat(path).st_size > MAX_FILE:
            raise ServiceError('结果文件超过 50 MiB。', code='invalid_arguments')
        with sftp.file(path, 'rb') as f:
            raw = f.read(MAX_FILE + 1)
    if len(raw)>MAX_FILE:
        raise ServiceError('结果文件超过大小限制。', code='invalid_arguments')
    name = 'result-' + re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', PurePosixPath(relative).name)[:120]
    folder = local_dir() / 'vlab-downloads' / uuid.uuid4().hex; folder.mkdir(parents=True)
    path = folder / name; path.write_bytes(raw)
    return {'path':str(path.resolve()), 'filename':name, 'sha256':hashlib.sha256(raw).hexdigest(), 'size_bytes':len(raw), 'downloaded':True}
