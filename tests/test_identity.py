from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from school_mcp.bb.identity import load_credentials, load_device_state, save_credentials, save_device_state, scoped_state, set_email_verification, status
from school_mcp.bb.login import try_submit_credentials
from school_mcp.bb.session import BBError


def cookie(name, domain="id.ustc.edu.cn", expires=None):
    return {"name": name, "value": "synthetic-device-token", "domain": domain, "path": "/", "expires": expires if expires is not None else time.time() + 86400, "httpOnly": True, "secure": True, "sameSite": "Lax"}


class IdentityScopeTests(unittest.TestCase):
    def test_retains_auth_state_and_excludes_unrelated_domains(self):
        state = scoped_state({"cookies": [cookie("trust"), cookie("parent", ".ustc.edu.cn"), cookie("other", "evil.example"), cookie("sibling", "other.ustc.edu.cn")], "origins": [
            {"origin": "https://id.ustc.edu.cn", "localStorage": [{"name": "device", "value": "synthetic-storage"}], "indexedDB": [{"name": "device-db"}]},
            {"origin": "https://evil.example"}, {"origin": "http://id.ustc.edu.cn"}, {"origin": "https://id.ustc.edu.cn:bad"}, {"origin": "https://user:secret@id.ustc.edu.cn"}]})
        self.assertEqual([c["name"] for c in state["cookies"]], ["trust", "parent"])
        self.assertEqual(len(state["origins"]), 1)
        self.assertTrue(state["cookies"][0]["httpOnly"])
        self.assertIn("indexedDB", state["origins"][0])

    def test_credentials_are_never_filled_on_other_hosts_or_http(self):
        for url in ("https://evil.example/", "https://www.bb.ustc.edu.cn/", "http://id.ustc.edu.cn/", "https://id.ustc.edu.cn.evil.example/", "https://id.ustc.edu.cn:444/", "https://user:secret@id.ustc.edu.cn/"):
            page = Mock(url=url)
            self.assertFalse(try_submit_credentials(page, {"username": "synthetic-user", "password": "synthetic-password"}))
            page.locator.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
class IdentityStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"SCHOOL_MCP_LOCAL_DIR": self.directory.name})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def test_credential_roundtrip_and_status_do_not_expose_secrets(self):
        save_credentials("synthetic-user", "synthetic-password")
        ciphertext = (Path(self.directory.name) / "ustc-identity.credentials.dpapi").read_bytes()
        self.assertNotIn(b"synthetic-password", ciphertext)
        self.assertEqual(load_credentials()["password"], "synthetic-password")
        self.assertTrue(status()["credentials_saved"])
        self.assertNotIn("synthetic-password", json.dumps(status()))

    def test_email_verification_requires_opt_in_and_resets_for_new_account(self):
        save_credentials("synthetic-user", "synthetic-password")
        self.assertFalse(status()["email_verification_enabled"])
        (Path(self.directory.name) / "mail.json").write_text(json.dumps({"address": "synthetic-user@mail.ustc.edu.cn"}), encoding="utf-8")
        set_email_verification(True)
        self.assertTrue(status()["email_verification_enabled"])
        self.assertEqual(load_credentials()["password"], "synthetic-password")
        self.assertEqual(load_credentials()["email_verification_address"], "synthetic-user@mail.ustc.edu.cn")
        set_email_verification(False)
        self.assertNotIn("email_verification_address", load_credentials())
        save_credentials("another-user", "synthetic-password")
        self.assertFalse(status()["email_verification_enabled"])

    def test_enable_without_mailbox_preserves_old_identity_credentials(self):
        save_credentials("synthetic-user", "synthetic-password")
        before = (Path(self.directory.name) / "ustc-identity.credentials.dpapi").read_bytes()
        with self.assertRaises(BBError):
            set_email_verification(True)
        self.assertEqual((Path(self.directory.name) / "ustc-identity.credentials.dpapi").read_bytes(), before)
        with self.assertRaises(BBError):
            save_credentials("another-user", "synthetic-new-password", email_verification=True)
        self.assertEqual((Path(self.directory.name) / "ustc-identity.credentials.dpapi").read_bytes(), before)

    def test_device_state_roundtrip_is_encrypted_and_account_bound(self):
        state = {"cookies": [cookie("trust")], "origins": [{"origin": "https://id.ustc.edu.cn", "localStorage": [{"name": "device", "value": "synthetic-storage"}]}]}
        save_device_state("synthetic-user", state, "chrome", {"locale": "zh-CN"})
        ciphertext = (Path(self.directory.name) / "ustc-identity.device.dpapi").read_bytes()
        self.assertNotIn(b"synthetic-device-token", ciphertext)
        self.assertNotIn(b"synthetic-storage", ciphertext)
        self.assertEqual(load_device_state("synthetic-user")["state"], state)
        with self.assertRaisesRegex(BBError, "不一致"):
            load_device_state("another-user")

    def test_bb_refresh_preserves_trust_state_and_drops_expired_old_cookies(self):
        save_credentials("synthetic-user", "synthetic-password")
        origin = {"origin": "https://id.ustc.edu.cn", "localStorage": [{"name": "device", "value": "synthetic-storage"}]}
        save_device_state("synthetic-user", {"cookies": [cookie("trust"), cookie("expired", expires=time.time()-20)], "origins": [origin]}, "chrome", {})
        saved = save_device_state("synthetic-user", {"cookies": [cookie("session", "www.bb.ustc.edu.cn")], "origins": []}, "chrome", {})
        self.assertEqual({c["name"] for c in saved["state"]["cookies"]}, {"trust", "session"})
        self.assertEqual(saved["state"]["origins"], [origin])
        result = status()
        self.assertEqual(result["identity_persistent_cookie_count"], 1)
        self.assertNotIn("synthetic-device-token", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
