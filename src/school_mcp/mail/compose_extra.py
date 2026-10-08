"""Prepare forwarding/reply-all using immutable existing outbox drafts."""
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses

from . import outbox
from .config import MailError
from .parsing import parse_email
from .selection import batch


def _save(client, payload, store):
    if payload.get('status') == 'needs_input':
        return payload
    store = store or outbox.Outbox()
    return outbox.receipt(store.create(payload), 'ready', payload, include_preview=True)


def forward(client, ref: dict, to: list[str], note: str = '', include_attachments: bool = True,
            cc: list[str] | None = None, attachments: list[str] | None = None, *, store=None) -> dict:
    raw, _ = batch(client, [ref])[0]
    original = BytesParser(policy=policy.default).parsebytes(raw)
    parsed = parse_email(raw, max_chars=len(raw) + 1)
    body = note + '\n\n---------- 转发邮件 ----------\n' + '\n'.join(
        f'{k}: {parsed[k]}' for k in ('from', 'to', 'cc', 'date', 'subject')) + '\n\n' + parsed['body']
    retained = []

    def visit(part):
        if part.get_filename() or part.get_content_disposition() == 'attachment' or part.get_content_maintype() not in {'multipart', 'text'}:
            data = part.get_payload(decode=True)
            mime = part.get_content_type()
            filename = part.get_filename() or f'attachment-{len(retained) + 1}'
            if data is None and mime == 'message/rfc822':
                # A MIME message attachment is kept as a readable .eml binary file;
                # do not walk into it and duplicate its nested attachments.
                data = b'\r\n'.join(p.as_bytes(policy=policy.SMTP) for p in part.get_payload())
                mime = 'application/octet-stream'
                if not filename.lower().endswith('.eml'):
                    filename += '.eml'
            if data is None:
                raise MailError('原附件无法完整保留；请明确排除附件或导出原始 EML。')
            retained.append((filename, mime, data))
        elif part.is_multipart():
            for child in part.iter_parts():
                visit(child)

    if include_attachments:
        visit(original)
    payload = outbox._compose(client.config, to=to, cc=cc, subject='Fwd: ' + parsed['subject'], body=body,
                              attachments=attachments, retained_attachments=retained)
    return _save(client, payload, store)


def reply_all(client, ref: dict, body: str, aliases: list[str] | None = None,
              attachments: list[str] | None = None, *, store=None) -> dict:
    raw, _ = batch(client, [ref])[0]
    message = BytesParser(policy=policy.default).parsebytes(raw)
    excluded = {client.config.address.lower()}
    for address in aliases or []:
        if not outbox.valid_address(address):
            raise MailError('自己的别名必须是完整邮箱地址。')
        excluded.add(address.lower())
    seen = set(excluded)

    def addresses(headers):
        result = []
        for _, addr in getaddresses(headers):
            if not outbox.valid_address(addr):
                raise MailError('原信存在无法可靠解析的收件地址，请人工指定收件人。')
            if addr.lower() not in seen:
                seen.add(addr.lower())
                result.append(addr)
        return result

    to = addresses(message.get_all('Reply-To') or message.get_all('From') or [])
    to += addresses(message.get_all('To') or [])
    cc = addresses(message.get_all('Cc') or [])
    parent = str(message.get('Message-ID', '')).strip()
    if not outbox.MESSAGE_ID.fullmatch(parent):
        raise MailError('原信缺少有效 Message-ID，不能自动构造线程回复。')
    references = str(message.get('References', '')).split()
    references = list(dict.fromkeys(references + [parent]))
    subject = str(message.get('Subject', ''))
    payload = outbox._compose(client.config, to=to, cc=cc, body=body,
        subject=subject if subject.lower().startswith('re:') else 'Re: ' + subject,
        attachments=attachments, reply_headers={'In-Reply-To': parent, 'References': ' '.join(references)})
    result = _save(client, payload, store)
    result['reply_source'] = {**ref, 'uses_reply_to_header': bool(message.get('Reply-To')), 'reply_all': True}
    return result
