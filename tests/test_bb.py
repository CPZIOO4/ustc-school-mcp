from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from school_mcp.bb.client import BBClient
from school_mcp.bb.parsing import course_links, page_data, safe_url
from school_mcp.bb.session import BBError, bb_cookies, load_session, save_session

PORTAL = '''<html><head><title>我的机构</title></head><body>
<a href="/webapps/blackboard/execute/launcher?type=Course&amp;id=_123_1">2026FA：量子力学</a>
<a href="/webapps/blackboard/execute/launcher?type=Course&amp;id=_123_1">进入课程</a>
<a href="https://evil.example/webapps/blackboard/execute/launcher?type=Course&amp;id=_999_1">外部课程</a>
</body></html>'''
COOKIES = [{"name": "JSESSIONID", "value": "synthetic-session-only", "domain": "www.bb.ustc.edu.cn", "path": "/", "secure": True}]


class BBParsingTests(unittest.TestCase):
    def test_course_ids_are_deduplicated_and_external_hosts_ignored(self):
        courses = course_links(PORTAL)
        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0]["course_id"], "_123_1")
        self.assertEqual(courses[0]["title"], "2026FA：量子力学")

    def test_page_text_omits_scripts_and_form_values(self):
        html = '<title>课程</title><main>通知<script>secret script</script><input value="private credential"><textarea>private draft</textarea><a href="/bbcswebdav/example.pdf">讲义</a></main>'
        result = page_data(html, "https://www.bb.ustc.edu.cn/webapps/blackboard/content/listContent.jsp", max_chars=2)
        self.assertEqual(result["text"], "通知")
        self.assertTrue(result["text_truncated"])
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(result["links"][0]["text"], "讲义")

    def test_urls_do_not_allow_other_hosts_http_or_embedded_credentials(self):
        for url in ('https://evil.example/x', 'http://www.bb.ustc.edu.cn/x', 'https://name:secret@www.bb.ustc.edu.cn/x', '//evil.example/x', '/x\r\nCookie: forged'):
            with self.assertRaises(BBError):
                safe_url(url)

    def test_identity_provider_cookies_are_not_exported(self):
        cookies = [*COOKIES, {"name": "CASTGC", "value": "synthetic-central-login", "domain": "id.ustc.edu.cn", "path": "/"}, {"name": "other", "value": "synthetic-other", "domain": "example.com"}]
        selected = bb_cookies(cookies)
        self.assertEqual([cookie["name"] for cookie in selected], ["JSESSIONID"])

    def test_course_menu_is_preserved_and_reset_and_tokens_removed(self):
        html = '<title>课程</title><div id="courseMenuPalette_contents"><a href="/webapps/blackboard/content/listContent.jsp?course_id=_1_1&amp;content_id=_2_1&amp;mode=view&amp;mode=reset&amp;csrf_token=synthetic-private">讲义</a></div><div id="contentPanel">课程主页</div>'
        result = page_data(html, "https://www.bb.ustc.edu.cn/webapps/blackboard/execute/modulepage/view")
        self.assertEqual(result["text"], "课程主页")
        self.assertTrue(result["links"][0]["course_menu"])
        self.assertNotIn("reset", result["links"][0]["path"])
        self.assertNotIn("synthetic-private", json.dumps(result))

    def test_empty_course_url_parameter_is_normalized(self):
        courses = course_links(PORTAL.replace('id=_123_1', 'id=_123_1&amp;url='))
        self.assertNotIn("url=", courses[0]["path"])


class BBRequestTests(unittest.TestCase):
    def test_logged_in_portal_is_read_over_https(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, headers={"Content-Type": "text/html; charset=UTF-8"}, text=PORTAL)
        client = BBClient(COOKIES, httpx.MockTransport(handle))
        self.assertEqual(client.courses()["count"], 1)
        self.assertEqual(calls[0].method, "GET")
        self.assertEqual(calls[0].url.scheme, "https")
        self.assertIn("JSESSIONID=synthetic-session-only", calls[0].headers["cookie"])

    def test_expired_session_redirect_does_not_send_cookies_to_identity_host(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(302, headers={"Location": "https://id.ustc.edu.cn/cas/login"})
        client = BBClient(COOKIES, httpx.MockTransport(handle))
        with self.assertRaisesRegex(BBError, "失效"):
            client.check()
        self.assertEqual(len(calls), 1)

    def test_login_html_is_not_treated_as_authenticated_portal(self):
        client = BBClient(COOKIES, httpx.MockTransport(lambda _: httpx.Response(200, headers={"Content-Type": "text/html"}, text='<form><input type="password"></form>')))
        with self.assertRaisesRegex(BBError, "失效"):
            client.check()

    def test_mutating_get_parameters_and_unknown_paths_are_blocked(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200)
        client = BBClient(COOKIES, httpx.MockTransport(handle))
        for path in ("/webapps/blackboard/execute/announcement?method=deleteAnnouncement", "/webapps/blackboard/execute/announcement?action=delete", "/webapps/logout/", "/webapps/blackboard/execute/announcement?cmd=delete"):
            with self.assertRaises(BBError):
                client.read_page(path)
        self.assertEqual(calls, [])


@unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
class BBSessionTests(unittest.TestCase):
    def test_session_is_encrypted_and_identity_cookies_are_omitted(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SCHOOL_MCP_LOCAL_DIR": directory}):
            save_session([*COOKIES, {"name": "CASTGC", "value": "central-secret-for-test", "domain": "id.ustc.edu.cn"}])
            ciphertext = (Path(directory) / "bb.session.dpapi").read_bytes()
            self.assertNotIn(b"synthetic-session-only", ciphertext)
            self.assertNotIn(b"central-secret-for-test", ciphertext)
            self.assertEqual(load_session()["cookies"][0]["value"], "synthetic-session-only")
            self.assertEqual(len(load_session()["cookies"]), 1)

    def test_missing_and_corrupt_sessions_have_safe_errors(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SCHOOL_MCP_LOCAL_DIR": directory}):
            with self.assertRaisesRegex(BBError, "尚未登录"):
                load_session()
            (Path(directory) / "bb.session.dpapi").write_bytes(b"synthetic-corrupt-data")
            with self.assertRaisesRegex(BBError, "尚未登录"):
                load_session()


if __name__ == "__main__":
    unittest.main()
