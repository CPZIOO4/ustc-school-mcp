from __future__ import annotations

import imaplib
import os
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from school_mcp.mail.client import MAX_MESSAGE_BYTES, MailClient
from school_mcp.mail.config import MailConfig, MailError
from school_mcp.mail.credentials import load_password, save_credentials
from school_mcp.mail.parsing import decode_mailbox, encode_mailbox, parse_email, parse_list_item


def sample_mail() -> bytes:
    message = EmailMessage()
    message["Subject"] = "学校通知：课程安排"
    message["From"] = "教务处 <office@example.edu>"
    message["To"] = "student@mail.ustc.edu.cn"
    message.set_content("周五下午开会。", charset="utf-8")
    message.add_alternative("<p>周五下午开会。</p>", subtype="html")
    message.add_attachment(b"example attachment", maintype="application", subtype="octet-stream", filename="../../通知.txt")
    return message.as_bytes()


class FakeIMAP:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.raw = sample_mail()
        self.size = len(self.raw)

    def login(self, address, password):
        self.calls.append(("LOGIN", address))
        return "OK", [b"logged in"]

    def logout(self):
        self.calls.append(("LOGOUT",))
        return "BYE", []

    def select(self, mailbox, readonly=False):
        self.calls.append(("SELECT", mailbox, readonly))
        return "OK", [b"2"]

    def response(self, key):
        return key, [b"43" if key == "UIDNEXT" else b"123"]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [b"17 42"]
        query = args[-1]
        if query == "(RFC822.SIZE)":
            return "OK", [f"1 (UID 42 RFC822.SIZE {self.size})".encode()]
        identifier = args[0].decode() if isinstance(args[0], bytes) else args[0]
        metadata = f"1 (UID {identifier} FLAGS () RFC822.SIZE {len(self.raw)})".encode()
        return "OK", [(metadata, self.raw), b")"]


class MailBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.config = MailConfig("student@mail.ustc.edu.cn")
        self.fake = FakeIMAP()
        self.mock = patch("school_mcp.mail.client.imaplib.IMAP4_SSL", return_value=self.fake)
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.client = MailClient(self.config, "test-only-not-a-real-password")

    def test_search_and_read_preserve_unread_and_paginate(self):
        result = self.client.search(limit=1)
        self.assertEqual(result["messages"][0]["uid"], 42)
        self.assertEqual(result["next_offset"], 1)
        self.assertEqual(result["uid_validity"], 123)
        self.assertEqual(result["messages"][0]["subject"], "学校通知：课程安排")
        body = self.client.read(42, 123)
        self.assertIn("周五下午开会", body["body"])
        self.assertTrue(all(call[2] is True for call in self.fake.calls if call[0] == "SELECT"))
        self.assertTrue(all("BODY.PEEK" in call[-1] for call in self.fake.calls if call[0] == "FETCH" and "BODY" in call[-1]))
        self.assertFalse(any(call[0] in {"STORE", "EXPUNGE", "APPEND"} for call in self.fake.calls))

    def test_changed_uid_validity_prevents_reading_wrong_mail(self):
        with self.assertRaisesRegex(MailError, "编号已改变"):
            self.client.read(42, 122)
        self.assertFalse(any(call[0] == "FETCH" for call in self.fake.calls))

    def test_code_snapshot_never_fetches_existing_mail(self):
        self.assertEqual(self.client.verification_snapshot(), {"uid_validity": 123, "next_uid": 43})
        self.assertFalse(any(call[0] in {"FETCH", "SEARCH"} for call in self.fake.calls))

    def test_code_headers_filter_old_uids_even_when_star_range_returns_them(self):
        self.assertEqual(self.client.verification_headers(43, 123)["messages"], [])
        self.assertFalse(any(call[0] == "FETCH" for call in self.fake.calls))
        search = next(call for call in self.fake.calls if call[0] == "SEARCH")
        self.assertEqual(search[1:3], ("UID", "43:*"))
        self.assertIn("FROM", search)
        self.assertIn("TO", search)

    def test_code_uid_change_stops_before_search_or_fetch(self):
        with self.assertRaises(MailError):
            self.client.verification_headers(43, 122)
        self.assertFalse(any(call[0] in {"SEARCH", "FETCH"} for call in self.fake.calls))

    def test_code_headers_and_limits_are_read_only(self):
        result = self.client.verification_headers(42, 123)
        self.assertEqual([m["uid"] for m in result["messages"]], [42])
        self.assertTrue(all("BODY.PEEK" in call[-1] for call in self.fake.calls if call[0] == "FETCH"))
        self.fake.uid = lambda *_: ("OK", [b" ".join(str(i).encode() for i in range(43, 65))])
        with self.assertRaisesRegex(MailError, "过多"):
            self.client.verification_headers(43, 123)

    def test_chinese_search_uses_utf8_and_escapes_quotes(self):
        self.client.search(subject='会议 "提醒"', unread_only=True)
        call = next(call for call in self.fake.calls if call[0] == "SEARCH")
        self.assertEqual(call[1:3], ("CHARSET", "UTF-8"))
        self.assertIn('"会议 \\"提醒\\""'.encode("utf-8"), call)

    def test_search_rejects_protocol_injection_and_invalid_dates(self):
        for params in ({"subject": "test\r\nLOGOUT"}, {"since": "2026-99-99"}, {"since": "2026-10-02", "before": "2026-10-01"}):
            with self.assertRaises(MailError):
                self.client.search(**params)
        self.assertEqual(self.fake.calls, [])

    def test_size_limit_stops_body_fetch(self):
        self.fake.size = MAX_MESSAGE_BYTES + 1
        with self.assertRaisesRegex(MailError, "20 MiB"):
            self.client.read(42, 123)
        self.assertFalse(any("BODY" in call[-1] for call in self.fake.calls if call[0] == "FETCH"))

    def test_attachment_path_cannot_escape_local_storage(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SCHOOL_MCP_LOCAL_DIR": directory}):
            attachment = parse_email(self.fake.raw)["attachments"][0]
            result = self.client.download(42, 123, attachment["part_index"])
            path = Path(result["path"])
            self.assertEqual(path.parent, Path(directory) / "attachments")
            self.assertEqual(path.read_bytes(), b"example attachment")
            self.assertEqual(result["size_bytes"], 18)

    def test_login_error_does_not_expose_credentials(self):
        self.fake.login = lambda *_: (_ for _ in ()).throw(imaplib.IMAP4.error("SERVER SAID test-only-not-a-real-password"))
        with self.assertRaises(MailError) as captured:
            self.client.read(42, 123)
        self.assertNotIn("test-only", str(captured.exception))


class ParsingTests(unittest.TestCase):
    def test_chinese_and_ampersand_mailboxes(self):
        for mailbox in ("收件箱", "已发送/学校", "A&B", 'folder "x"', "INBOX"):
            self.assertEqual(decode_mailbox(encode_mailbox(mailbox)), mailbox)
        item = parse_list_item(b'(\\HasNoChildren) "/" "&XfJT0ZAB-"')
        self.assertEqual(item["name"], "已发送")
        self.assertTrue(item["selectable"])

    def test_literal_mailbox_names(self):
        item = parse_list_item((b'(\\Noselect) "/" {5}', b"A &-B"))
        self.assertEqual(item["name"], "A &B")
        self.assertFalse(item["selectable"])

    def test_plain_body_and_attachment_are_not_conflated(self):
        parsed = parse_email(sample_mail(), max_chars=3)
        self.assertEqual(parsed["body"], "周五下")
        self.assertTrue(parsed["body_truncated"])
        self.assertEqual(len(parsed["attachments"]), 1)
        self.assertEqual(parsed["attachments"][0]["filename"], "../../通知.txt")

    def test_html_conversion_does_not_include_script_or_style(self):
        message = EmailMessage()
        message.set_content("<html><head><style>hidden css</style></head><body><p>学校通知 &amp; 会议</p><script>hidden javascript</script></body></html>", subtype="html")
        body = parse_email(message.as_bytes())["body"]
        self.assertIn("学校通知 & 会议", body)
        self.assertNotIn("hidden", body)

    def test_attached_message_body_is_not_main_body(self):
        message = EmailMessage()
        message.set_content("main body")
        attached = EmailMessage()
        attached.set_content("attached private body")
        message.add_attachment(attached, filename="forwarded.eml")
        parsed = parse_email(message.as_bytes())
        self.assertIn("main body", parsed["body"])
        self.assertNotIn("attached private body", parsed["body"])
        self.assertEqual(parsed["attachments"][0]["content_type"], "message/rfc822")


@unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
class CredentialTests(unittest.TestCase):
    def test_dpapi_roundtrip_and_account_binding(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SCHOOL_MCP_LOCAL_DIR": directory, "SCHOOL_MAIL_PASSWORD": ""}):
            config = MailConfig("student@mail.ustc.edu.cn")
            secret = "synthetic-secret-for-test"
            save_credentials(config, secret)
            self.assertEqual(load_password(config), secret)
            self.assertNotIn(secret.encode(), (Path(directory) / "mail.credentials.dpapi").read_bytes())
            with self.assertRaisesRegex(MailError, "不一致"):
                load_password(MailConfig("other@mail.ustc.edu.cn"))


if __name__ == "__main__":
    unittest.main()
