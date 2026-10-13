"""Real Rust-owned headless Chrome, with synthetic pages and no school login."""
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from school_mcp.browser import chrome_browser
from school_mcp.desktop import native_core


class NativeBrowserBoundaryTests(unittest.TestCase):
    def test_native_error_has_typed_recovery_reason(self):
        from school_mcp.desktop.model import ModelError
        from school_mcp.recovery import classify
        from school_mcp.service_errors import ServiceError
        for native_reason, expected in [("browser_unavailable", "runtime_failure"), ("browser_busy", "login_busy"), ("invalid_arguments", "invalid_arguments")]:
            with self.subTest(native_reason=native_reason), patch.object(native_core, "enabled", return_value=True), patch.object(native_core, "call", side_effect=ModelError(native_reason, "synthetic private path")):
                with self.assertRaises(ServiceError) as raised:
                    with chrome_browser(Mock(), headless=True):
                        pass
                self.assertEqual(classify(raised.exception), (expected, None))
                self.assertNotIn("synthetic private path", str(raised.exception))

    def test_connect_failure_stops_owned_process(self):
        playwright = Mock()
        playwright.chromium.connect_over_cdp.side_effect = ValueError("synthetic connect failure")
        owned = {"browser_id": "synthetic", "endpoint": "http://127.0.0.1:12345"}
        with patch.object(native_core, "enabled", return_value=True), patch.object(native_core, "call", side_effect=[owned, {"stopped": True}]) as call:
            with self.assertRaisesRegex(ValueError, "synthetic connect failure"):
                with chrome_browser(playwright, headless=True):
                    self.fail("must not execute a site operation")
        self.assertEqual(call.call_args_list[-1].args, ("browser_stop", {"browser_id": "synthetic"}))
        playwright.chromium.launch.assert_not_called()

    def test_close_failure_still_stops_owned_process(self):
        playwright = Mock()
        playwright.chromium.connect_over_cdp.return_value.close.side_effect = ValueError("synthetic close failure")
        owned = {"browser_id": "synthetic", "endpoint": "http://127.0.0.1:12345"}
        with patch.object(native_core, "enabled", return_value=True), patch.object(native_core, "call", side_effect=[owned, {}]) as call:
            with self.assertRaisesRegex(ValueError, "synthetic close failure"):
                with chrome_browser(playwright, headless=True):
                    pass
        self.assertEqual(call.call_args_list[-1].args[0], "browser_stop")

    def test_native_path_cannot_silently_fall_back(self):
        playwright = Mock()
        with patch.object(native_core, "enabled", return_value=True), patch.object(native_core, "call", side_effect=RuntimeError("native stopped")):
            with self.assertRaisesRegex(RuntimeError, "native stopped"):
                with chrome_browser(playwright):
                    pass
        playwright.chromium.launch.assert_not_called()

    def test_cleanup_errors_preserve_original_site_failure(self):
        playwright = Mock()
        playwright.chromium.connect_over_cdp.return_value.close.side_effect = RuntimeError("close failed")
        owned = {"browser_id": "synthetic", "endpoint": "http://127.0.0.1:12345"}
        with patch.object(native_core, "enabled", return_value=True), patch.object(native_core, "call", side_effect=[owned, RuntimeError("stop failed")]):
            with self.assertRaisesRegex(ValueError, "original site failure"):
                with chrome_browser(playwright, headless=True):
                    raise ValueError("original site failure")

    def test_background_operation_overrides_explicit_login_setting(self):
        playwright = Mock()
        owned = {"browser_id": "synthetic", "endpoint": "http://127.0.0.1:12345"}
        with patch.dict(os.environ, {"SCHOOL_MCP_BROWSER_HEADED": "1"}), patch.object(native_core, "enabled", return_value=True), patch.object(native_core, "call", side_effect=[owned, {}]) as call:
            with chrome_browser(playwright, headless=True):
                pass
        self.assertEqual(call.call_args_list[0].args, ("browser_start", {"headless": True}))


def endpoint_closed(endpoint):
    parsed = urlsplit(endpoint)
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.2):
            return False
    except OSError:
        return True


def wait_closed(endpoint):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if endpoint_closed(endpoint):
            return True
        time.sleep(0.05)
    return False


@unittest.skipUnless(os.environ.get("SCHOOL_NATIVE_CORE"), "requires built native core")
class NativeBrowserLiveTests(unittest.TestCase):
    def test_real_headless_dom_and_exact_profile_cleanup(self):
        from playwright.sync_api import sync_playwright
        starts = []
        original_call = native_core.call

        def record(operation, value):
            result = original_call(operation, value)
            if operation == "browser_start":
                starts.append(result)
            return result

        profile = None
        with patch.object(native_core, "call", record), sync_playwright() as playwright:
            with chrome_browser(playwright, headless=True) as browser:
                context = browser.new_context()
                page = context.new_page()
                page.set_content('<title>Rust browser synthetic</title><input aria-label="value"><button onclick="document.title=\'done\'">Continue</button>')
                page.get_by_label("value").fill("synthetic only")
                page.get_by_role("button", name="Continue").click()
                self.assertEqual(page.title(), "done")
                self.assertIn("HeadlessChrome", page.evaluate("navigator.userAgent"))
                second_context = browser.new_context()
                second_page = second_context.new_page()
                second_page.set_content("<title>independent context</title>")
                port = str(urlsplit(starts[0]["endpoint"]).port)
                for candidate in Path(tempfile.gettempdir()).glob("school-core-chrome-*"):
                    try:
                        if (candidate / "DevToolsActivePort").read_text().splitlines()[0] == port:
                            profile = candidate
                    except (OSError, IndexError):
                        pass
                self.assertIsNotNone(profile)
                context.close()
                self.assertEqual(second_page.title(), "independent context")
                second_context.close()
        self.assertTrue(wait_closed(starts[0]["endpoint"]))
        self.assertFalse(profile.exists())
        self.assertFalse(original_call("browser_status", {"browser_id": starts[0]["browser_id"]})["running"])

    def test_stopping_one_owned_browser_preserves_another(self):
        first = native_core.call("browser_start", {})
        second = None
        try:
            second = native_core.call("browser_start", {})
            native_core.call("browser_stop", {"browser_id": first["browser_id"]})
            self.assertTrue(wait_closed(first["endpoint"]))
            self.assertTrue(native_core.call("browser_status", {"browser_id": second["browser_id"]})["running"])
            self.assertFalse(endpoint_closed(second["endpoint"]))
        finally:
            native_core.call("browser_stop", {"browser_id": first["browser_id"]})
            if second:
                native_core.call("browser_stop", {"browser_id": second["browser_id"]})

    @unittest.skipUnless(os.name == "nt", "Windows Job Object ownership")
    def test_core_crash_closes_only_its_browser_job(self):
        process = subprocess.Popen([os.environ["SCHOOL_NATIVE_CORE"], "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        owned = None
        profile = None
        try:
            raw = json.dumps({"operation": "browser_start", "input": {}}).encode()
            process.stdin.write(struct.pack(">I", len(raw)) + raw)
            process.stdin.flush()
            size = struct.unpack(">I", process.stdout.read(4))[0]
            result = json.loads(process.stdout.read(size))
            self.assertTrue(result["ok"], result)
            owned = result["result"]
            port = str(urlsplit(owned["endpoint"]).port)
            for candidate in Path(tempfile.gettempdir()).glob("school-core-chrome-*"):
                try:
                    if (candidate / "DevToolsActivePort").read_text().splitlines()[0] == port:
                        profile = candidate
                except (OSError, IndexError):
                    pass
            process.kill()
            process.wait(timeout=5)
            self.assertTrue(wait_closed(owned["endpoint"]))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
            # Abrupt termination cannot run Rust Drop; remove only this captured synthetic profile.
            if profile is not None and profile.exists():
                import shutil
                import uuid
                deadline = time.monotonic() + 5
                while profile.exists():
                    # Re-check the exact captured directory before every recursive attempt.
                    self.assertFalse(profile.is_symlink() or profile.is_junction())
                    self.assertEqual(profile.resolve().parent, Path(tempfile.gettempdir()).resolve())
                    self.assertTrue(profile.name.startswith("school-core-chrome-"))
                    uuid.UUID(profile.name.removeprefix("school-core-chrome-"))
                    try:
                        shutil.rmtree(profile)
                        break
                    except OSError as exc:
                        # The CDP port can close before Windows releases descendant file handles.
                        if exc.winerror not in {5, 32, 145} or time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
