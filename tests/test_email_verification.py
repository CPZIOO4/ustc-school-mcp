from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from school_mcp.bb.email_verification import EmailVerification, MailCodeRequest, extract_code, matches_address
from school_mcp.mail.config import MailError

ADDRESS = "synthetic-user@mail.ustc.edu.cn"


def verification_mail(uid=43, code="654321"):
    return {"uid": uid, "uid_validity": 123, "from": "中科大统一认证 <id@ustc.edu.cn>", "to": ADDRESS,
            "subject": "统一身份认证验证码", "body": f"您的登录验证码为：{code}，仅用于本次统一身份认证。"}


def mailbox(messages):
    client = Mock(config=SimpleNamespace(address=ADDRESS))
    client.verification_snapshot.return_value = {"uid_validity": 123, "next_uid": 43}
    client.verification_headers.return_value = {"uid_validity": 123, "messages": messages}
    client.read.side_effect = lambda uid, *args, **kwargs: next(m for m in messages if m["uid"] == uid)
    return client


class MailCodeTests(unittest.TestCase):
    def test_masked_target_must_match_the_connected_mailbox(self):
        self.assertTrue(matches_address(ADDRESS, "synthetic-****@mail.ustc.edu.cn"))
        self.assertFalse(matches_address(ADDRESS, "someone-****@mail.ustc.edu.cn"))
        self.assertFalse(matches_address(ADDRESS, "synthetic-****@other.example"))
        self.assertFalse(matches_address(ADDRESS, "****@mail.ustc.edu.cn"))
        self.assertFalse(matches_address(ADDRESS, "synthetic-****@*.ustc.edu.cn"))

    def test_ignores_old_codes_and_other_senders_and_recipients(self):
        old = verification_mail(uid=42)
        wrong_sender = {**verification_mail(uid=44), "from": "id@ustc.edu.cn.evil.example"}
        wrong_recipient = {**verification_mail(uid=45), "to": "other@mail.ustc.edu.cn"}
        client = mailbox([wrong_recipient, wrong_sender, verification_mail(), old])
        request = MailCodeRequest(client)
        self.assertEqual(request.poll(), "654321")
        self.assertEqual(client.read.call_count, 1)
        self.assertEqual(client.read.call_args.args, (43, 123))

    def test_ambiguous_code_mail_is_not_guessed(self):
        with self.assertRaisesRegex(MailError, "多封"):
            MailCodeRequest(mailbox([verification_mail(), verification_mail(uid=44, code="123456")])).poll()
        self.assertIsNone(extract_code({**verification_mail(), "body": "统一认证验证码：654321；校验码：123456"}, ADDRESS))

    def test_mailbox_uid_change_stops_reading(self):
        client = mailbox([])
        client.verification_headers.return_value = {"uid_validity": 124, "messages": [verification_mail()]}
        with self.assertRaisesRegex(MailError, "编号"):
            MailCodeRequest(client).poll()
        client.read.assert_not_called()

    def test_unrelated_numbers_and_non_identity_codes_are_rejected(self):
        self.assertIsNone(extract_code({**verification_mail(), "body": "统一身份认证，请联系 654321 查询资料。"}, ADDRESS))
        self.assertIsNone(extract_code({**verification_mail(), "subject": "其他网站验证码", "body": "验证码：654321"}, ADDRESS))
        self.assertIsNone(extract_code({**verification_mail(), "body_truncated": True}, ADDRESS))

    def test_new_ambiguous_or_truncated_identity_message_stops_instead_of_waiting(self):
        for message in ({**verification_mail(), "body_truncated": True}, {**verification_mail(), "body": "验证码：654321；验证码：123456"}):
            with self.subTest(message_type=message.get("body_truncated", False)):
                with self.assertRaises(MailError):
                    MailCodeRequest(mailbox([message])).poll()


class VerificationBrowserTests(unittest.TestCase):
    """Isolated browser behavior; not evidence of a live school challenge."""

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)
        except PlaywrightError:
            cls.playwright.stop()
            raise unittest.SkipTest("Installed Chrome is required for the isolated browser fixture")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.addCleanup(self.context.close)
        self.notifications = Mock()
        self.trust_device = Mock()
        self.verifier = EmailVerification(True, self.notifications, self.trust_device, expected_address=ADDRESS)

    def show(self, html, host="id.ustc.edu.cn"):
        url = f"https://{host}/test-verification"
        self.page.route(url, lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html))
        self.page.goto(url)

    @staticmethod
    def fixture(address=ADDRESS):
        return f'''<input type="password" value="fixture-only"><div role="dialog">
        <h2>可信邮箱验证</h2><p>{address}</p>
        <input name="emailCode" placeholder="请输入验证码">
        <button onclick="window.requests=(window.requests||0)+1">获取验证码</button>
        <button onclick="window.submissions=(window.submissions||0)+1">验证</button></div>'''

    def test_request_then_read_and_submit_once_without_logging_code(self):
        self.show(self.fixture())
        client = mailbox([verification_mail()])
        with patch("school_mcp.bb.email_verification.load_config", return_value=client.config), patch("school_mcp.bb.email_verification.MailClient", return_value=client):
            self.verifier.step(self.page)
            self.verifier.step(self.page)
            self.verifier.step(self.page)
        self.assertEqual(self.page.locator('input[name="emailCode"]').input_value(), "654321")
        self.assertEqual(self.page.evaluate("window.requests"), 1)
        self.assertEqual(self.page.evaluate("window.submissions"), 1)
        self.trust_device.assert_called_once()
        self.assertIsNone(self.verifier.pending_code)
        self.assertNotIn("654321", repr(self.notifications.mock_calls))
        self.verifier.observe_result("验证码已过期，请重新获取")
        self.verifier.step(self.page)
        self.assertTrue(self.verifier.blocked)
        self.assertEqual(self.page.evaluate("window.requests"), 1)
        self.assertEqual(self.page.evaluate("window.submissions"), 1)

    def test_wait_timeout_does_not_resend_or_read_old_mail(self):
        self.show(self.fixture())
        client = mailbox([])
        with patch("school_mcp.bb.email_verification.load_config", return_value=client.config), patch("school_mcp.bb.email_verification.MailClient", return_value=client):
            self.verifier.step(self.page)
            self.verifier.deadline = 0
            self.verifier.step(self.page)
            self.verifier.step(self.page)
        self.assertTrue(self.verifier.blocked)
        self.assertEqual(self.page.evaluate("window.requests"), 1)
        client.verification_snapshot.assert_called_once()
        client.verification_headers.assert_not_called()
        client.read.assert_not_called()

    def test_other_recipient_and_other_host_never_request_email(self):
        with patch("school_mcp.bb.email_verification.load_config", return_value=SimpleNamespace(address=ADDRESS)), patch("school_mcp.bb.email_verification.MailClient") as mail:
            self.show(self.fixture("other-***@mail.ustc.edu.cn"))
            self.verifier.step(self.page)
            mail.assert_not_called()
            self.assertTrue(self.verifier.blocked)
            self.assertIsNone(self.page.evaluate("window.requests"))
            self.verifier = EmailVerification(True, self.notifications, self.trust_device, expected_address=ADDRESS)
            self.show(self.fixture(), host="evil.example")
            self.verifier.step(self.page)
            mail.assert_not_called()

    def test_changed_target_or_config_after_request_never_reads_or_submits(self):
        for change in ("page", "config", "origin"):
            with self.subTest(change=change):
                self.verifier = EmailVerification(True, self.notifications, self.trust_device, expected_address=ADDRESS)
                self.show(self.fixture())
                client = mailbox([verification_mail()])
                with patch("school_mcp.bb.email_verification.load_config", return_value=client.config) as config, patch("school_mcp.bb.email_verification.MailClient", return_value=client):
                    self.verifier.step(self.page)
                    if change == "page":
                        self.page.locator("p").evaluate("el => el.textContent = 'other@mail.ustc.edu.cn'")
                    elif change == "config":
                        config.return_value = SimpleNamespace(address="other@mail.ustc.edu.cn")
                    else:
                        self.show(self.fixture(), host="evil.example")
                    self.verifier.step(self.page)
                client.verification_headers.assert_not_called()
                self.assertTrue(self.verifier.blocked)
                self.assertIsNone(self.page.evaluate("window.submissions"))

    def test_legacy_unbound_policy_requires_explicit_local_binding(self):
        self.verifier = EmailVerification(True, self.notifications, self.trust_device)
        self.show(self.fixture())
        with patch("school_mcp.bb.email_verification.load_config", return_value=SimpleNamespace(address=ADDRESS)), patch("school_mcp.bb.email_verification.MailClient") as mail:
            self.verifier.step(self.page)
        self.assertTrue(self.verifier.blocked)
        mail.assert_not_called()

    def test_multiple_different_page_addresses_never_request_code(self):
        self.show(self.fixture().replace(f"<p>{ADDRESS}</p>", f"<p>{ADDRESS}，或 other@mail.ustc.edu.cn</p>"))
        with patch("school_mcp.bb.email_verification.load_config", return_value=SimpleNamespace(address=ADDRESS)), patch("school_mcp.bb.email_verification.MailClient") as mail:
            self.verifier.step(self.page)
        mail.assert_not_called()
        self.assertTrue(self.verifier.blocked)
        self.assertIsNone(self.page.evaluate("window.requests"))

    def test_captcha_requires_manual_completion_and_disabled_policy_does_nothing(self):
        with patch("school_mcp.bb.email_verification.MailClient") as mail:
            self.show(self.fixture().replace('<input name="emailCode"', '<input name="captcha" placeholder="图形验证码"><input name="emailCode"'))
            self.verifier.step(self.page)
            mail.assert_not_called()
            self.assertFalse(self.verifier.requested)
            self.verifier = EmailVerification(False, self.notifications, self.trust_device)
            self.show(self.fixture())
            self.verifier.step(self.page)
            mail.assert_not_called()


if __name__ == "__main__":
    unittest.main()
