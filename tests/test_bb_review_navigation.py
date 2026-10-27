"""Real Chrome/local slow server: waiting must not resend an assignment GET."""
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import sync_playwright, TimeoutError
from school_mcp.bb.assignment_review import wait_for_review, review_request_allowed


class ReviewNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def scenario(self, delay, budget, cancel=False):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                time.sleep(delay)
                self.send_response(200)
                self.end_headers()
                try:
                    self.wfile.write(b'<h1 id="pageTitleHeader">Review Submission History: test</h1>')
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        context = self.browser.new_context(java_script_enabled=False)
        page = context.new_page()
        target = f'http://127.0.0.1:{server.server_port}/history'
        context.route('**/*', lambda route: route.continue_() if review_request_allowed(
            route.request.url, route.request.method, target,
            navigation=route.request.is_navigation_request(),
            main_frame=route.request.frame == page.main_frame) else route.abort())
        progress = []
        def stage(text):
            progress.append(text)
            if cancel and len(progress) > 1:
                raise ValueError('stopped')
        started = time.monotonic()
        try:
            if cancel:
                with self.assertRaisesRegex(ValueError, 'stopped'):
                    wait_for_review(page, target, stage, timeout_seconds=budget)
                self.assertLess(time.monotonic() - started, 2)
            elif delay > budget:
                with self.assertRaises(TimeoutError):
                    wait_for_review(page, target, stage, timeout_seconds=budget)
            else:
                response = wait_for_review(page, target, stage, timeout_seconds=budget)
                self.assertEqual(response.status, 200)
                self.assertIn('Review Submission History', page.locator('h1').inner_text())
            self.assertEqual(requests, ['/history'])
            self.assertGreaterEqual(len(progress), 2)
        finally:
            context.close()
            server.shutdown()
            server.server_close()

    def test_slow_response_completes_without_replay(self):
        self.scenario(1.6, 4)

    def test_cancellation_during_wait(self):
        self.scenario(3, 5, cancel=True)

    def test_deadline_stops_without_replay(self):
        self.scenario(3, 1.5)
