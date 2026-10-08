from __future__ import annotations

from network_test_support import isolate_pacing as setUpModule

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from school_mcp import login_runtime as runtime
from school_mcp.browser import chrome_browser


class LoginRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {"SCHOOL_MCP_LOCAL_DIR": self.directory.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def saved(self, stage, pid=123):
        runtime.progress_path("bb").write_text(json.dumps({"stage": stage}), encoding="utf-8")
        (Path(self.directory.name) / "bb-login.pid").write_text(str(pid), encoding="utf-8")

    def test_exited_worker_cannot_look_like_ongoing_login(self):
        self.saved("authenticating")
        with patch.object(runtime, "running", return_value=False):
            result = runtime.progress("bb")
        self.assertEqual(result["login_progress"]["stage"], "interrupted")
        self.assertFalse(result["login_running"])
        self.assertIn("school_bb_reconnect", result["next_step"])
        self.assertEqual(json.loads(runtime.progress_path("bb").read_text())["stage"], "authenticating")

    def test_running_worker_and_historical_success_are_distinct(self):
        self.saved("authenticating")
        with patch.object(runtime, "running", return_value=True):
            result = runtime.progress("bb")
        self.assertTrue(result["login_running"])
        self.assertEqual(result["login_progress"]["stage"], "authenticating")
        self.saved("connected")
        with patch.object(runtime, "running", return_value=False):
            result = runtime.progress("bb")
        self.assertEqual(result["login_progress"]["stage"], "connected")
        self.assertFalse(result["network_checked"])
        self.assertEqual(result["connection_state"], "unchecked")

    def test_missing_or_corrupt_status_is_not_success(self):
        for contents in ('[]', '{', '{"stage": 1}'):
            runtime.progress_path("bb").write_text(contents, encoding="utf-8")
            self.assertIsNone(runtime.progress("bb")["login_progress"])

    def test_manual_challenge_does_not_offer_repeated_background_attempt(self):
        self.saved("waiting_for_verification")
        with patch.object(runtime, "running", return_value=False):
            result = runtime.progress("bb")
        self.assertIn("--headed", result["next_step"])
        self.assertNotIn("school_bb_reconnect", result["next_step"])

    def test_reconnect_forces_headless_and_returns_poll_tool(self):
        notify = Mock()
        with patch.dict(os.environ, {"SCHOOL_MCP_BROWSER_HEADED": "1"}), patch.object(runtime.subprocess, "Popen", return_value=Mock(pid=123)) as spawn:
            result = runtime.start_login("jw", notify, RuntimeError)
        self.assertEqual(spawn.call_args.kwargs["env"]["SCHOOL_MCP_BROWSER_HEADED"], "0")
        self.assertEqual(result["status_tool"], "school_jw_status")
        self.assertTrue(result["headless"])
        self.assertEqual(runtime.worker_pid("jw"), 123)

    def test_another_service_login_prevents_device_state_race(self):
        self.saved("authenticating")
        with patch.object(runtime, "running", return_value=True), patch.object(runtime.subprocess, "Popen") as spawn:
            result = runtime.start_login("jw", Mock(), RuntimeError)
        self.assertFalse(result["started"])
        self.assertEqual(result["busy_service"], "bb")
        self.assertEqual(result["status_tool"], "school_bb_auth_status")
        spawn.assert_not_called()

    def test_recycled_pid_cannot_block_login_or_appear_running(self):
        self.saved('connected')
        recorded = (Path(self.directory.name) / 'bb-login.pid').stat().st_mtime
        with patch.object(runtime, 'running', return_value=True), \
             patch.object(runtime, 'process_started_at', return_value=recorded + 3600), \
             patch.object(runtime.subprocess, 'Popen', return_value=Mock(pid=124)) as spawn:
            self.assertFalse(runtime.progress('bb')['login_running'])
            self.assertTrue(runtime.start_login('jw', Mock(), RuntimeError)['started'])
            spawn.assert_called_once()

    def test_original_process_creation_retains_live_lock(self):
        self.saved('authenticating')
        recorded = (Path(self.directory.name) / 'bb-login.pid').stat().st_mtime
        with patch.object(runtime, 'running', return_value=True), \
             patch.object(runtime, 'process_started_at', return_value=recorded - 1):
            self.assertTrue(runtime.worker_running('bb', 123))

    def test_spawn_failure_reports_error_not_permanent_starting(self):
        notify = Mock()
        with patch.object(runtime.subprocess, "Popen", side_effect=OSError("synthetic-private-path")):
            with self.assertRaises(RuntimeError) as raised:
                runtime.start_login("jw", notify, RuntimeError)
        self.assertEqual(notify.call_args.args[0], "error")
        self.assertNotIn("synthetic-private-path", str(raised.exception))

    def test_launch_lock_prevents_simultaneous_spawns(self):
        with runtime.launch_lock(), patch.object(runtime.subprocess, "Popen") as spawn:
            result = runtime.start_login("jw", Mock(), RuntimeError)
        self.assertFalse(result["started"])
        spawn.assert_not_called()

    def test_forced_background_read_ignores_visible_login_setting(self):
        browser = Mock()
        with patch.dict(os.environ, {"SCHOOL_MCP_BROWSER_HEADED": "1"}):
            with chrome_browser(browser, headless=True):
                pass
        browser.chromium.launch.assert_called_once_with(channel="chrome", headless=True)


if __name__ == "__main__":
    unittest.main()
