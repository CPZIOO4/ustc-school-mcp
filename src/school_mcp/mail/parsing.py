from __future__ import annotations

import base64
import re
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser

from .config import MailError


def encode_mailbox(name: str) -> str:
    """IMAP modified UTF-7 (RFC 3501), used by traditional Coremail servers."""
    chunks: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            encoded = base64.b64encode("".join(pending).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            chunks.append("&" + encoded + "-")
            pending.clear()

    for char in name:
        if " " <= char <= "~":
            flush()
            chunks.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return "".join(chunks)


def decode_mailbox(name: str) -> str:
    def decode(match: re.Match[str]) -> str:
        value = match.group(1)
        if not value:
            return "&"
        value = value.replace(",", "/")
        return base64.b64decode(value + "=" * (-len(value) % 4)).decode("utf-16-be")
    return re.sub(r"&([^-]*)-", decode, name)


def quote(value: str) -> str:
    if any(c in value for c in "\r\n\x00"):
        raise MailError("参数不能包含换行或空字符。")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_list_item(item: bytes | tuple) -> dict:
    line = item[0] if isinstance(item, tuple) else item
    match = re.match(rb'^\((.*?)\)\s+(NIL|"(?:[^"\\]|\\.)*")\s+(.+)$', line)
    if not match:
        raise MailError("无法解析服务器返回的邮件文件夹。")
    raw_name = item[1] if isinstance(item, tuple) else match.group(3)
    name = raw_name.decode("ascii")
    if name.startswith('"') and name.endswith('"'):
        name = re.sub(r"\\(.)", r"\1", name[1:-1])
    flags = match.group(1).decode("ascii").split()
    return {"name": decode_mailbox(name), "flags": flags, "selectable": "\\Noselect" not in flags}


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style", "head"}:
            self.hidden.append(tag)
        if not self.hidden and tag in {"p", "div", "br", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()
        if not self.hidden and tag in {"p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def parse_email(raw: bytes, *, include_body: bool = True, max_chars: int = 20000) -> dict:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    result = {key: str(message.get(key, "")) for key in ("subject", "from", "to", "cc", "date", "message-id", "in-reply-to", "reply-to", "references")}
    if not include_body:
        return result
    attachments: list[dict] = []
    for index, part in enumerate(message.walk()):
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        # Message/rfc822 attachments are listed, never flattened into the main body.
        if disposition == "attachment" or filename:
            payload = part.get_payload(decode=True)
            attachments.append({"part_index": index, "filename": filename or f"attachment-{index}", "content_type": part.get_content_type(), "size_bytes": len(payload) if payload is not None else None})
            continue
    body_part = message.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        payload = body_part.get_payload(decode=True) or b""
        try:
            body = payload.decode(body_part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            body = payload.decode("utf-8", errors="replace")
    if body_part is not None and body_part.get_content_type() == "text/html":
        converter = _HTMLText()
        converter.feed(body)
        body = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(converter.parts)).strip()
    result.update(body=body[:max_chars], body_truncated=len(body) > max_chars, body_total_chars=len(body), attachments=attachments, content_is_untrusted=True)
    return result
