from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from email.utils import formatdate
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from school_mcp import network


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "policy.sqlite3"
        self.clock = Clock()
        self.policy = network.RequestLimiter(self.path, clock=self.clock, sleep=self.clock.sleep, jitter=lambda: 0)
        replacement = patch.object(network, "limiter", self.policy)
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_instances_share_spacing_but_services_are_independent(self):
        other = network.RequestLimiter(self.path, clock=self.clock, sleep=self.clock.sleep)
        self.policy.acquire("bb")
        other.acquire("jw")
        other.acquire("bb")
        self.assertEqual(self.clock.waits, [1.0])

    def test_exponential_cooldown_is_shared_and_early_calls_do_not_extend_it(self):
        for expected in [2, 4, 8, 16, 32, 60, 60]:
            self.assertEqual(self.policy.failure("bb"), expected)
            with self.assertRaises(network.CooldownError) as caught:
                self.policy.acquire("bb")
            self.assertEqual(caught.exception.retry_after_seconds, expected)
            self.clock.now += expected
        self.policy.success("bb")
        self.assertEqual(self.policy.failure("bb"), 2)

    def test_inflight_success_does_not_cancel_another_requests_cooldown(self):
        self.policy.failure("jw", server_wait=120)
        self.policy.success("jw")
        with self.assertRaises(network.CooldownError) as caught:
            self.policy.acquire("jw")
        self.assertEqual(caught.exception.retry_after_seconds, 120)

    def test_retry_after_seconds_and_dates_are_not_capped_to_backoff(self):
        self.assertEqual(network.retry_after("3600", 1000), 3600)
        self.assertEqual(network.retry_after(formatdate(4600, usegmt=True), 1000), 3600)
        for value in [None, "garbage", "-1", "nan", "inf"]:
            self.assertEqual(network.retry_after(value, 1000), 0)
        with self.assertRaises(network.CooldownError) as caught:
            self.policy.response("teach", 503, {"retry-after": "3600"})
        self.assertEqual(caught.exception.retry_after_seconds, 3600)
        self.assertEqual(self.clock.waits, [])

    def test_http_429_is_not_replayed_and_future_call_is_blocked_before_io(self):
        requests = []
        response = httpx.Response(429, headers={"Retry-After": "90"})
        def handler(request):
            requests.append(request)
            return response
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "90 秒"):
                with network.http_client("teach", ValueError, transport=httpx.MockTransport(handler)) as client:
                    client.get("https://www.teach.ustc.edu.cn/")
        self.assertEqual(len(requests), 1)
        self.assertTrue(response.is_closed)

    def test_all_six_http_adapters_use_the_shared_policy(self):
        from school_mcp.bb.client import BBClient, PORTAL_PATH, BBError
        from school_mcp.jw.client import JWClient, JWError
        from school_mcp.library.client import LibraryClient, LibraryError
        from school_mcp.teach.client import TeachClient, TeachError
        from school_mcp.icourse.client import ICourseClient, ICourseError
        from school_mcp.nan7.client import Nan7Client, Nan7Error
        cases = [
            (lambda t: BBClient(cookies=[], transport=t).get(PORTAL_PATH), BBError),
            (lambda t: JWClient(cookies=[], transport=t).get("/home"), JWError),
            (lambda t: LibraryClient(cookies=[], base_url="http://opac.lib.ustc.edu.cn", transport=t).get("/reader/book_lst.php"), LibraryError),
            (lambda t: TeachClient(t).list_notices(), TeachError),
            (lambda t: ICourseClient(t).list_courses(), ICourseError),
            (lambda t: Nan7Client("synthetic-token", t).search(), Nan7Error),
        ]
        for call, error in cases:
            with self.subTest(adapter=error.__name__):
                handler = Mock(return_value=httpx.Response(429, headers={"Retry-After": "60"}))
                for _ in range(2):
                    with self.assertRaisesRegex(error, "60 秒"):
                        call(httpx.MockTransport(handler))
                self.assertEqual(handler.call_count, 1)

    def test_young_navigation_429_stops_without_a_second_browser(self):
        from school_mcp.young.client import YoungClient, YoungError
        page = Mock()
        page.goto.return_value = Mock(status=429, headers={"retry-after": "60"})
        browser = Mock()
        browser.new_context.return_value.new_page.return_value = page
        with patch("school_mcp.young.client.load_session", return_value={"state": {"cookies": [], "origins": []}}), patch("school_mcp.young.client.sync_playwright"), patch("school_mcp.young.client.chrome_browser") as launch:
            launch.return_value.__enter__.return_value = browser
            for _ in range(2):
                with self.assertRaisesRegex(YoungError, "60 秒"):
                    YoungClient().home()
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(page.goto.call_count, 1)

    def test_auth_failure_is_not_replayed_and_records_a_minute_cooldown(self):
        with network.http_client("bb", ValueError, transport=httpx.MockTransport(lambda r: httpx.Response(403))) as client:
            self.assertEqual(client.get("https://www.bb.ustc.edu.cn/").status_code, 403)
        with self.assertRaises(network.CooldownError) as caught:
            self.policy.acquire("bb")
        self.assertEqual(caught.exception.retry_after_seconds, 60)

    def test_login_html_failure_does_not_reset_backoff_on_http_200(self):
        from school_mcp.bb.client import BBClient, BBError, PORTAL_PATH
        handler = Mock(side_effect=lambda r: httpx.Response(200, headers={"content-type": "text/html"}, text='<input type="password">'))
        client = BBClient(cookies=[], transport=httpx.MockTransport(handler))
        for expected in [2, 4]:
            with self.assertRaisesRegex(BBError, "会话"):
                client.get(PORTAL_PATH)
            with self.assertRaisesRegex(BBError, f"{expected} 秒"):
                client.get(PORTAL_PATH)
            self.clock.now += expected
        self.assertEqual(handler.call_count, 2)

    def test_redirects_are_each_paced(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "/two"}) if r.url.path == "/one" else httpx.Response(200))
        with network.http_client("bb", ValueError, transport=transport, follow_redirects=False) as client:
            client.get("https://www.bb.ustc.edu.cn/one")
            client.get("https://www.bb.ustc.edu.cn/two")
        self.assertEqual(self.clock.waits, [1])

    def test_stream_failure_is_not_retried_or_reset_by_success_headers(self):
        class Broken(httpx.SyncByteStream):
            def __iter__(self):
                yield b"partial"
                raise httpx.ReadError("synthetic-private-detail")
        for wait in [2, 4]:
            with self.assertRaisesRegex(ValueError, f"{wait} 秒") as caught:
                with network.http_client("icourse", ValueError, transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Broken()))) as client:
                    with client.stream("GET", "https://icourse.club/course/") as response:
                        response.read()
            self.assertNotIn("synthetic-private", str(caught.exception))
            self.clock.now += wait

    def test_database_error_fails_closed_without_network(self):
        self.path.mkdir()
        called = Mock()
        with self.assertRaisesRegex(ValueError, "停止网络"):
            with network.http_client("jw", ValueError, transport=httpx.MockTransport(called)) as client:
                client.get("https://jw.ustc.edu.cn/home")
        called.assert_not_called()

    def test_mail_setup_import_does_not_require_http_or_mcp_packages(self):
        source = str(Path(__file__).resolve().parents[1] / "src")
        script = "import sys; sys.path.insert(0, sys.argv[1]); from school_mcp.mail.onboarding import connect_and_save"
        subprocess.run([sys.executable, "-S", "-c", script, source], capture_output=True, timeout=15, check=True)

    def test_new_process_observes_cooldown_and_database_contains_only_policy_state(self):
        # Wall-clock expiry permits an independent interpreter to verify persistence.
        real = network.RequestLimiter(self.path, jitter=lambda: 0)
        real.failure("bb", server_wait=120)
        script = "from pathlib import Path; from school_mcp.network import RequestLimiter,CooldownError; import sys\ntry: RequestLimiter(Path(sys.argv[1])).acquire('bb')\nexcept CooldownError as e: print(e.retry_after_seconds)\nelse: raise SystemExit(2)"
        result = subprocess.run([sys.executable, "-c", script, str(self.path)], capture_output=True, text=True, timeout=15, check=True)
        self.assertGreater(int(result.stdout), 90)
        with closing(sqlite3.connect(self.path)) as conn:
            columns = [x[1] for x in conn.execute("PRAGMA table_info(pacing)")]
        self.assertEqual(columns, ["service", "next_at", "failures", "blocked_until"])

    def test_imap_auth_failure_is_not_retried_and_blocks_next_login(self):
        import imaplib
        from school_mcp.mail.client import MailClient
        from school_mcp.mail.config import MailConfig, MailError
        connection = Mock()
        connection.login.side_effect = imaplib.IMAP4.error("synthetic-private-detail")
        with patch("school_mcp.mail.client.imaplib.IMAP4_SSL", return_value=connection) as create:
            for _ in range(2):
                with self.assertRaises(MailError):
                    MailClient(MailConfig("student@mail.ustc.edu.cn"), "synthetic-password").check()
            self.assertEqual(create.call_count, 1)
            self.assertEqual(connection.login.call_count, 1)

    def test_each_imap_read_command_is_paced(self):
        from school_mcp.mail.client import PacedIMAP
        connection = Mock()
        connection.select.return_value = ("OK", [])
        connection.uid.return_value = ("OK", [])
        connection.response.return_value = ("UIDVALIDITY", [b"1"])
        client = PacedIMAP(connection)
        client.select("INBOX", readonly=True)
        client.response("UIDVALIDITY")  # cached: no request and no extra delay
        client.uid("SEARCH", "ALL")
        self.assertEqual(self.clock.waits, [0.5])

    def test_reconnect_stops_before_spawning_during_shared_identity_cooldown(self):
        from school_mcp import login_runtime
        self.policy.failure("identity", authentication=True)
        with patch.object(login_runtime, "local_dir", return_value=Path(self.directory.name)), patch.object(login_runtime.subprocess, "Popen") as spawn:
            result = login_runtime.start_login("bb", Mock(), ValueError)
        self.assertFalse(result["started"])
        self.assertEqual(result["retry_after_seconds"], 60)
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
