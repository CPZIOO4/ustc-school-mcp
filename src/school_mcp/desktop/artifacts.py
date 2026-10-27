"""Expose only files produced by known tools in this chat, never arbitrary paths."""
import hashlib
from pathlib import Path
import uuid

OUTPUTS = {'school_digest_export_calendar': 'calendar-exports', 'school_mail_download_attachment': 'attachments', 'school_mail_export': 'mail-exports',
           'school_web_download': 'web-downloads', 'school_vlab_download': 'vlab-downloads', 'school_drive_download':'drive-downloads', 'school_compute_collect_experiment':'compute-downloads'}
MAX_BYTES = 60 * 1024 * 1024
DIRECTORIES = set(OUTPUTS.values()) | {'generated', 'web-downloads', 'drive-downloads', 'vlab-downloads'}


def checked_bytes(data, record):
    path = Path(record['path']).resolve()
    if (record.get('directory') not in DIRECTORIES
            or not path.is_relative_to((data.profile / record['directory']).resolve()) or not path.is_file()
            or path.stat().st_size > MAX_BYTES):
        raise ValueError('文件已失效，请重新获取。')
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != record['sha256']:
        raise ValueError('文件内容已变化，请重新获取。')
    return raw


def register(data, chat, tool, response):
    directory = OUTPUTS.get(tool)
    result = response.get('result')
    if (not directory or response.get('dispatched') is not True or response.get('tool') != tool
            or not isinstance(result, dict) or not result.get('path') or not result.get('sha256')):
        return
    path = str(Path(result['path']).resolve())
    record = dict(id=uuid.uuid4().hex, path=path, directory=directory, sha256=result['sha256'])
    raw = checked_bytes(data, record)
    # Names are presentation text, never markup or response header source.
    name = str(result.get('filename') or Path(path).name).replace('\\', '/').split('/')[-1]
    record.update(name=''.join(c for c in name if c.isprintable())[:180] or 'download', size_bytes=len(raw))
    artifacts = chat.setdefault('artifacts', [])
    if not any(a['path'] == path and a['sha256'] == record['sha256'] for a in artifacts):
        artifacts.append(record)
    if path not in chat['allowed_files']:
        chat['allowed_files'].append(path)


def public(chat):
    return [{k: a[k] for k in ('id', 'name', 'size_bytes')} for a in chat.get('artifacts', [])]


def register_local(data, chat, path, directory, name):
    if directory not in DIRECTORIES:
        raise ValueError('不支持的输出目录。')
    path = Path(path).resolve()
    if not path.is_relative_to((data.profile / directory).resolve()) or not path.is_file():
        raise ValueError('输出文件不在本机材料目录中。')
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('输出文件过大。')
    record = dict(id=uuid.uuid4().hex, path=str(path), directory=directory,
                  sha256=hashlib.sha256(raw).hexdigest(), name=name, size_bytes=len(raw))
    checked_bytes(data, record)
    chat.setdefault('artifacts', []).append(record)
    if str(path) not in chat['allowed_files']:
        chat['allowed_files'].append(str(path))
    return record


def download(data, chat_id, artifact_id):
    chat = data.chat(chat_id)
    record = next((a for a in chat.get('artifacts', []) if a['id'] == artifact_id), None)
    if record is None:
        raise ValueError('该文件不属于当前对话。')
    return checked_bytes(data, record)
