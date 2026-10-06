from __future__ import annotations

import hashlib
import imaplib
import re
import socket
import ssl
import uuid
from contextlib import contextmanager
from datetime import date
from email import policy
from email.parser import BytesParser
from typing import Iterator

from .config import MailConfig, MailError, local_dir
from .credentials import load_password
from .parsing import encode_mailbox, parse_email, parse_list_item, quote
from .. import network

MAX_MESSAGE_BYTES = 20 * 1024 * 1024


class PacedIMAP:
    """Pace actual IMAP commands; cached response() and cleanup do not send reads."""
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        method = getattr(self.connection, name)
        if name not in {"uid", "select", "list", "status"}:
            return method

        def call(*args, **kwargs):
            network.limiter.acquire("mail")
            result = method(*args, **kwargs)
            if result[0] != "OK":
                network.limiter.failure("mail")
            return result
        return call


def _ok(status: str, data: list, action: str) -> list:
    if status != "OK":
        raise MailError(f"邮件服务器未能完成{action}。")
    return data


class MailClient:
    def __init__(self, config: MailConfig, password: str | None = None):
        self.config = config
        self.password = password

    @contextmanager
    def session(self) -> Iterator[imaplib.IMAP4_SSL]:
        password = self.password if self.password is not None else load_password(self.config)
        connection = None
        try:
            network.limiter.acquire("mail")
            connection = imaplib.IMAP4_SSL(self.config.host, self.config.port, ssl_context=ssl.create_default_context(), timeout=self.config.timeout)
            try:
                network.limiter.acquire("mail")
                connection.login(self.config.address, password)
            except imaplib.IMAP4.error:
                network.limiter.failure("mail", authentication=True)
                raise MailError("邮箱登录失败。请检查完整地址、客户端专用密码和 IMAP 是否已启用。") from None
            yield PacedIMAP(connection)
            network.limiter.success("mail")
        except network.PolicyError as exc:
            raise MailError(str(exc)) from None
        except MailError:
            raise
        except ssl.SSLError:
            network.limiter.failure("mail", authentication=True)
            raise MailError("邮箱 TLS 证书验证或加密连接失败。") from None
        except (socket.timeout, TimeoutError):
            network.limiter.failure("mail")
            raise MailError("连接邮箱超时，请稍后重试。") from None
        except OSError:
            network.limiter.failure("mail")
            raise MailError("无法连接学校邮箱，请检查网络。") from None
        except (imaplib.IMAP4.error, UnicodeError):
            network.limiter.failure("mail")
            raise MailError("邮件服务器拒绝操作或返回了无法解析的数据。") from None
        finally:
            if connection is not None:
                try:
                    connection.logout()
                except (OSError, imaplib.IMAP4.error):
                    pass

    @staticmethod
    def select(connection: imaplib.IMAP4_SSL, mailbox: str, expected: int | None = None) -> tuple[int, int]:
        if not mailbox or len(mailbox) > 512:
            raise MailError("邮件文件夹名称无效。")
        data = _ok(*connection.select(quote(encode_mailbox(mailbox)), readonly=True), "打开文件夹")
        _, validity_data = connection.response("UIDVALIDITY")
        try:
            validity = int(validity_data[0])
            count = int(data[0])
        except (TypeError, ValueError, IndexError):
            raise MailError("服务器未提供有效的邮件 UIDVALIDITY。") from None
        if expected is not None and expected != validity:
            raise MailError("文件夹的邮件编号已改变，请重新查询邮件后再读取。")
        return validity, count

    def check(self) -> dict:
        with self.session() as connection:
            validity, count = self.select(connection, "INBOX")
            data = _ok(*connection.status('"INBOX"', "(UNSEEN)"), "查询未读数量")
            match = re.search(rb"UNSEEN (\d+)", data[0] or b"")
            return {"connected": True, "mailbox": "INBOX", "uid_validity": validity, "messages": count, "unread": int(match.group(1)) if match else None, "read_only": True}

    def folders(self) -> dict:
        with self.session() as connection:
            data = _ok(*connection.list(), "查询文件夹")
            return {"folders": [parse_list_item(item) for item in data if item]}

    def verification_snapshot(self) -> dict:
        """Capture the next UID without fetching any existing message headers."""
        with self.session() as connection:
            validity, _ = self.select(connection, "INBOX")
            _, data = connection.response("UIDNEXT")
            try:
                next_uid = int(data[0])
                if next_uid < 1:
                    raise ValueError
            except (TypeError, ValueError, IndexError):
                raise MailError("服务器未提供验证码请求所需的 UIDNEXT，请人工完成验证。") from None
            return {"uid_validity": validity, "next_uid": next_uid}

    def verification_headers(self, first_uid: int, uid_validity: int, limit: int = 20) -> dict:
        """Bounded headers of only new school-sender messages to this mailbox."""
        if type(first_uid) is not int or first_uid < 1 or type(limit) is not int or not 1 <= limit <= 20:
            raise MailError("验证码邮件查询边界无效。")
        with self.session() as connection:
            validity, _ = self.select(connection, "INBOX", expected=uid_validity)
            data = _ok(*connection.uid("SEARCH", "UID", f"{first_uid}:*", "OR", "FROM", '"@ustc.edu.cn"', "FROM", '"@mail.ustc.edu.cn"', "TO", quote(self.config.address)), "查询本次认证邮件")
            try:
                # IMAP n:* can include the last old message if n exceeds the current maximum.
                identifiers = sorted({int(value) for value in (data[0] or b"").split() if int(value) >= first_uid}, reverse=True)
            except (TypeError, ValueError, IndexError):
                raise MailError("服务器返回了无效的认证邮件编号。") from None
            if len(identifiers) > limit:
                raise MailError("新的认证邮件候选过多，请人工确认本次验证码。")
            messages = []
            for uid in identifiers:
                fetched = _ok(*connection.uid("FETCH", str(uid), "(UID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO CC DATE MESSAGE-ID)])"), "读取本次认证邮件头")
                parts = [item for item in fetched if isinstance(item, tuple)]
                if not parts:
                    continue
                metadata, headers = parts[0]
                match = re.search(rb"UID (\d+)", metadata)
                if match is None or int(match.group(1)) != uid:
                    raise MailError("认证邮件编号与请求不一致，请人工完成验证。")
                message = parse_email(headers, include_body=False)
                message.update(uid=uid, uid_validity=validity)
                messages.append(message)
            return {"uid_validity": validity, "messages": messages}

    @staticmethod
    def _query(*, unread_only: bool, sender: str, subject: str, text: str, since: str, before: str) -> list:
        query: list = ["UNSEEN"] if unread_only else ["ALL"]
        for key, value in (("FROM", sender), ("SUBJECT", subject), ("TEXT", text)):
            if value:
                if len(value) > 1000:
                    raise MailError("搜索条件过长。")
                query.extend([key, quote(value)])
        for key, value in (("SINCE", since), ("BEFORE", before)):
            if value:
                try:
                    parsed = date.fromisoformat(value)
                except ValueError:
                    raise MailError("搜索日期请使用 YYYY-MM-DD 格式。") from None
                months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
                query.extend([key, f"{parsed.day:02d}-{months[parsed.month - 1]}-{parsed.year}"])
        if since and before and date.fromisoformat(since) >= date.fromisoformat(before):
            raise MailError("since 应早于 before；before 当天不包含在搜索范围内。")
        return query

    def search(self, mailbox: str = "INBOX", limit: int = 10, offset: int = 0, unread_only: bool = False, sender: str = "", subject: str = "", text: str = "", since: str = "", before: str = "", include_headers: bool = False) -> dict:
        if not 1 <= limit <= 50 or not 0 <= offset <= 1000000:
            raise MailError("limit 范围为 1–50，offset 必须为 0–1000000。")
        criteria = self._query(unread_only=unread_only, sender=sender, subject=subject, text=text, since=since, before=before)
        utf8 = any(not item.isascii() for item in criteria)
        args = ["CHARSET", "UTF-8", *[item.encode("utf-8") for item in criteria]] if utf8 else criteria
        with self.session() as connection:
            validity, _ = self.select(connection, mailbox)
            status, data = connection.uid("SEARCH", *args)
            if status != "OK":
                raise MailError("服务器未能完成搜索；若包含中文，请改用日期或发件人条件尝试。")
            identifiers = (data[0] or b"").split()
            selected = list(reversed(identifiers))[offset:offset + limit]
            messages = []
            for identifier in selected:
                fields = "SUBJECT FROM TO CC DATE MESSAGE-ID IN-REPLY-TO" if include_headers else "SUBJECT FROM DATE"
                fetched = _ok(*connection.uid("FETCH", identifier, f"(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({fields})])"), "读取邮件摘要")
                parts = [item for item in fetched if isinstance(item, tuple)]
                if not parts:
                    # A concurrently removed message may disappear between SEARCH and FETCH.
                    continue
                metadata, headers = parts[0]
                flags = re.search(rb"FLAGS \(([^)]*)\)", metadata)
                size = re.search(rb"RFC822.SIZE (\d+)", metadata)
                message = parse_email(headers, include_body=False)
                if not include_headers:
                    message = {k: message[k] for k in ("subject", "from", "date")}
                message.update(uid=int(identifier), uid_validity=validity, mailbox=mailbox, flags=flags.group(1).decode().split() if flags else [], size_bytes=int(size.group(1)) if size else None)
                messages.append(message)
            return {"mailbox": mailbox, "uid_validity": validity, "total_matches": len(identifiers), "offset": offset, "next_offset": offset + limit if offset + limit < len(identifiers) else None, "order": "uid_descending", "messages": messages, "content_is_untrusted": True}

    @staticmethod
    def _raw(connection: imaplib.IMAP4_SSL, uid: int) -> tuple[bytes, list[str]]:
        if not isinstance(uid, int) or uid < 1:
            raise MailError("邮件 UID 必须是正整数。")
        info = _ok(*connection.uid("FETCH", str(uid), "(RFC822.SIZE)"), "查询邮件大小")
        size_match = next((re.search(rb"RFC822.SIZE (\d+)", item) for item in info if isinstance(item, bytes) and b"RFC822.SIZE" in item), None)
        if not size_match:
            raise MailError("邮件不存在或无法读取邮件大小。")
        if int(size_match.group(1)) > MAX_MESSAGE_BYTES:
            raise MailError("此邮件超过第一版的 20 MiB 读取限制，请在网页邮箱中查看。")
        data = _ok(*connection.uid("FETCH", str(uid), "(UID FLAGS BODY.PEEK[])"), "读取邮件")
        for item in data:
            if isinstance(item, tuple):
                metadata, raw = item
                if len(raw) > MAX_MESSAGE_BYTES:
                    raise MailError("邮件内容超过读取限制。")
                flags = re.search(rb"FLAGS \(([^)]*)\)", metadata)
                return raw, flags.group(1).decode().split() if flags else []
        raise MailError("邮件已不存在，请重新查询。")

    def read(self, uid: int, uid_validity: int, mailbox: str = "INBOX", max_chars: int = 6000, include_headers: bool = False) -> dict:
        if not 1 <= max_chars <= 100000:
            raise MailError("max_chars 范围为 1–100000。")
        with self.session() as connection:
            self.select(connection, mailbox, expected=uid_validity)
            raw, flags = self._raw(connection, uid)
        result = parse_email(raw, max_chars=max_chars)
        if not include_headers:
            for key in ("to", "cc", "message-id", "in-reply-to"):
                result.pop(key, None)
        result.update(uid=uid, uid_validity=uid_validity, mailbox=mailbox, flags=flags)
        return result

    def download(self, uid: int, uid_validity: int, part_index: int, mailbox: str = "INBOX") -> dict:
        if not 1 <= part_index <= 1000:
            raise MailError("请选择读取邮件结果中的有效附件 part_index。")
        with self.session() as connection:
            self.select(connection, mailbox, expected=uid_validity)
            raw, _ = self._raw(connection, uid)
        message = BytesParser(policy=policy.default).parsebytes(raw)
        parts = list(message.walk())
        if part_index >= len(parts):
            raise MailError("附件编号不存在。")
        part = parts[part_index]
        if not (part.get_filename() or part.get_content_disposition() == "attachment"):
            raise MailError("指定的 MIME 部分不是附件。")
        payload = part.get_payload(decode=True)
        if payload is None:
            if part.get_content_type() == "message/rfc822":
                payload = b"\r\n".join(nested.as_bytes(policy=policy.SMTP) for nested in part.get_payload())
            else:
                raise MailError("此附件格式暂不支持下载。")
        original = part.get_filename() or f"attachment-{part_index}"
        filename = original.replace("\\", "/").split("/")[-1]
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(". ")[:100] or "attachment"
        mailbox_id = hashlib.sha256(mailbox.encode()).hexdigest()[:8]
        directory = local_dir() / "attachments"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{mailbox_id}-{uid_validity}-{uid}-{part_index}-{uuid.uuid4().hex[:8]}-{filename}"
        if not path.resolve().is_relative_to(directory.resolve()):
            raise MailError("附件保存路径无效。")
        try:
            with path.open("xb") as target:
                target.write(payload)
        except OSError:
            raise MailError("无法将附件保存到本地目录。") from None
        return {"path": str(path.resolve()), "filename": original, "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "content_is_untrusted": True}
