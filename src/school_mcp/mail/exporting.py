"""Private, bounded exports. EML preserves the exact fetched MIME bytes."""
import hashlib
import json
import uuid
import zipfile
from io import BytesIO

from .config import MailError, local_dir
from .parsing import parse_email
from .selection import batch, reference


def export(client, messages: list[dict], format: str = 'eml') -> dict:
    if format not in {'eml', 'txt', 'md'}:
        raise MailError('导出格式必须为 eml、txt 或 md。')
    snapshots = batch(client, messages)
    files, manifest = [], []
    for index, (raw, snap) in enumerate(snapshots, 1):
        name = f'{index:02d}-message-{snap["uid"]}.{format}'
        if format == 'eml':
            data = raw
        else:
            parsed = parse_email(raw, max_chars=len(raw) + 1)
            text = '\n'.join(f'{k}: {parsed[k]}' for k in ('subject', 'from', 'to', 'cc', 'date'))
            text += '\n\n' + parsed['body']
            text += '\n\n附件清单（不含附件字节；需要完整内容请导出 EML）：\n'
            text += '\n'.join(a['filename'] or '(unnamed)' for a in parsed['attachments'])
            if format == 'md':
                # Escape untrusted markup, including images/links/raw HTML.
                text = ''.join('\\' + c if c in '\\`*_{}[]<>()#+-.!|~' else c for c in text)
            data = text.encode('utf-8')
        files.append((name, data))
        manifest.append({**reference(snap), 'file': name, 'source_sha256': snap['sha256'],
                         'export_sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)})
    if len(files) == 1:
        suffix, data = format, files[0][1]
    else:
        output = BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, contents in files:
                archive.writestr(name, contents)
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        suffix, data = 'zip', output.getvalue()
    directory = local_dir() / 'mail-exports'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'mail-{uuid.uuid4().hex}.{suffix}'
    with path.open('xb') as stream:
        stream.write(data)
    return {'path': str(path.resolve()), 'format': suffix, 'message_count': len(files),
            'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'includes_attachment_bytes': format == 'eml', 'content_is_untrusted': True}
