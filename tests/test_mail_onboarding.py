from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from school_mcp.mail.config import MailConfig, MailError, load_config
from school_mcp.mail.credentials import load_password, save_credentials
from school_mcp.mail.onboarding import connect_and_save, local_status, setup_guide

ADDRESS = "synthetic-user@mail.ustc.edu.cn"
SECRET = "synthetic-only-client-secret"


class MailOnboardingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        environment = patch.dict(os.environ, {"SCHOOL_MCP_LOCAL_DIR": directory.name, "SCHOOL_MAIL_PASSWORD": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def config(self, value):
        (self.directory / "mail.json").write_text(json.dumps(value), encoding="utf-8")

    def test_guide_is_private_and_does_not_open_window_or_use_network(self):
        with patch("school_mcp.mail.client.imaplib.IMAP4_SSL") as imap, patch("webbrowser.open") as browser, patch("subprocess.Popen") as process:
            result = setup_guide()
        self.assertEqual(result["state"], "setup_required")
        self.assertFalse(result["opens_window"])
        self.assertFalse(result["network_checked"])
        imap.assert_not_called()
        browser.assert_not_called()
        process.assert_not_called()

    def test_status_does_not_decrypt_or_expose_account_and_is_unchecked(self):
        self.config({"address": ADDRESS})
        self.assertEqual(local_status()["state"], "credential_required")
        (self.directory / "mail.credentials.dpapi").write_bytes(b"synthetic-corrupt-ciphertext")
        with patch("school_mcp.mail.credentials._dpapi") as decrypt:
            result = setup_guide()
        decrypt.assert_not_called()
        self.assertEqual(result["state"], "configured_unchecked")
        self.assertFalse(result["credentials_checked"])
        self.assertNotIn(ADDRESS, json.dumps(result))

    def test_invalid_config_types_have_safe_errors(self):
        for value in (None, [], {}, 123, ["synthetic"], {"address": []}, {"address": None}, {"address": 123}):
            self.config(value)
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(MailError):
                    load_config()
                self.assertEqual(local_status()["state"], "setup_required")

    def test_connect_saves_only_after_read_only_validation(self):
        client = Mock()
        client.check.return_value = {"messages": 2, "unread": 1}
        calls = []
        client.check.side_effect = lambda: calls.append("check") or {"messages": 2, "unread": 1}
        with patch("school_mcp.mail.onboarding.MailClient", return_value=client), patch("school_mcp.mail.onboarding.save_credentials", side_effect=lambda *_: calls.append("save")):
            result = connect_and_save(ADDRESS, SECRET)
        self.assertEqual(calls, ["check", "save"])
        self.assertTrue(result["saved"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(ADDRESS, json.dumps(result))

    def test_failed_imap_check_preserves_existing_files(self):
        self.config({"address": ADDRESS})
        credential = self.directory / "mail.credentials.dpapi"
        credential.write_bytes(b"synthetic-old-ciphertext")
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        with patch("school_mcp.mail.onboarding.MailClient") as client, patch("school_mcp.mail.onboarding.save_credentials") as save:
            client.return_value.check.side_effect = MailError("synthetic login failure")
            with self.assertRaises(MailError):
                connect_and_save("other@mail.ustc.edu.cn", SECRET)
        save.assert_not_called()
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)

    def test_invalid_input_fails_before_network(self):
        with patch("school_mcp.mail.onboarding.MailClient") as client:
            for address, password in (("invalid", SECRET), (ADDRESS, "")):
                with self.assertRaises(MailError):
                    connect_and_save(address, password)
            client.assert_not_called()

    def test_save_failure_is_sanitized(self):
        with patch("school_mcp.mail.onboarding.MailClient") as client, patch("school_mcp.mail.onboarding.save_credentials", side_effect=OSError(SECRET)):
            client.return_value.check.return_value = {"messages": 2, "unread": 1}
            with self.assertRaises(MailError) as captured:
                connect_and_save(ADDRESS, SECRET)
        self.assertNotIn(SECRET, str(captured.exception))

    def test_config_commit_failure_rolls_back_ciphertext(self):
        config = self.directory / "mail.json"
        credential = self.directory / "mail.credentials.dpapi"
        config.write_text(json.dumps({"address": ADDRESS}), encoding="utf-8")
        credential.write_bytes(b"synthetic-old-ciphertext")
        before = (config.read_bytes(), credential.read_bytes())
        original_replace = Path.replace

        def replace(source, target):
            if source.name == "mail.json.new":
                raise OSError("synthetic write failure")
            return original_replace(source, target)

        with patch("school_mcp.mail.credentials._dpapi", return_value=b"synthetic-new-ciphertext"), patch.object(Path, "replace", replace):
            with self.assertRaisesRegex(MailError, "保留旧配置"):
                save_credentials(MailConfig("other@mail.ustc.edu.cn"), SECRET)
        self.assertEqual((config.read_bytes(), credential.read_bytes()), before)
        self.assertEqual({path.name for path in self.directory.iterdir()}, {"mail.json", "mail.credentials.dpapi"})

    def test_corrupt_password_type_is_not_returned(self):
        (self.directory / "mail.credentials.dpapi").write_bytes(b"synthetic-ciphertext")
        with patch("school_mcp.mail.credentials._dpapi", return_value=json.dumps({"address": ADDRESS, "password": 123}).encode()):
            with self.assertRaises(MailError):
                load_password(MailConfig(ADDRESS))


if __name__ == "__main__":
    unittest.main()
