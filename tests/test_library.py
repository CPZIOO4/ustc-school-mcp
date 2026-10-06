from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from school_mcp.bb.identity import save_credentials
from school_mcp.library.client import LibraryClient, list_services
from school_mcp.library.login import find_sso_url
from school_mcp.library.parsing import book_records, summary
from school_mcp.library.session import HOST, LibraryError, load_session, save_session

COOKIES = [{"name": "PHPSESSID", "value": "synthetic-reader-session", "domain": HOST, "path": "/", "secure": False}]
AUTH = '<a href="logout.php">注销</a>'
HISTORY = AUTH + '''<div id="mylib_content"><form><input name="csrf_token" value="synthetic-private-token"></form>
<table id="tableSort"><tr><td></td><td>条码号</td><td>题名</td><td>责任者</td><td>借阅日期 ⇅</td><td>归还日期 ⇅</td><td>馆藏地</td></tr>
<tr><td><input value="synthetic-private-control"></td><td>TEST-123</td><td><a href="../opac/item.php?marc_no=synthetic-id">测试图书</a><script>synthetic-private-script</script></td><td>测试作者</td><td>2026-09-01</td><td>2026-09-10</td><td>测试馆</td></tr></table></div>'''


class LibraryParsingTests(unittest.TestCase):
    def test_history_returns_book_fields_and_omits_tokens_and_controls(self):
        result = book_records(HISTORY, "loan_history")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["records"][0]["title"], "测试图书")
        self.assertEqual(result["records"][0]["returned_date"], "2026-09-10")
        self.assertNotIn("synthetic-", json.dumps(result))

    def test_current_loans_keep_due_date_without_renewal_actions(self):
        html = AUTH + '<div id="mylib_content"><table><tr><th>题名/责任者</th><th>借阅日期</th><th>应还日期</th><th>续借</th></tr><tr><td>测试书 / 作者</td><td>2026-09-01</td><td>2026-10-01</td><td><button>续借</button></td></tr></table></div>'
        result = book_records(html, "current_loans")
        self.assertEqual(result["records"][0]["due_date"], "2026-10-01")
        self.assertNotIn("续借", json.dumps(result, ensure_ascii=False))

    def test_empty_loans_ignore_renewal_captcha_dialog_and_unknown_is_not_empty(self):
        html = AUTH + '<div id="mylib_content"><strong class="iconerr">您的该项记录为空！</strong><div id="dialog-form"><table><tr><td>验证码：</td></tr></table></div></div>'
        self.assertEqual(book_records(html, "current_loans")["count"], 0)
        with self.assertRaisesRegex(LibraryError, "不能判定"):
            book_records(AUTH + '<div id="mylib_content">服务维护中</div>', "current_loans")

    def test_summary_preserves_blank_counts_and_omits_email(self):
        html = '<div class="infobox"><div class="infobox-content">超期图书</div><span class="infobox-data-number">0</span></div><div class="infobox"><div class="infobox-content">荐购图书</div><span class="infobox-data-number"></span></div><div class="profile-info-row"><div class="profile-info-name">Email：</div><div class="profile-info-value">synthetic-private-address@example.com</div></div>'
        result = summary(html)
        self.assertEqual(result["statistics"][0]["count"], 0)
        self.assertIsNone(result["statistics"][1]["count"])
        self.assertNotIn("synthetic-private", json.dumps(result))


class LibraryRequestTests(unittest.TestCase):
    def test_http_cookie_scope_and_secure_attribute_are_preserved(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text=AUTH)
        cookies = [*COOKIES, {"name": "TLS_ONLY", "value": "synthetic-tls-secret", "domain": HOST, "path": "/", "secure": True}, {"name": "CASTGC", "value": "synthetic-sso-secret", "domain": "id.ustc.edu.cn", "path": "/"}, {"name": "device", "value": "synthetic-parent-secret", "domain": ".ustc.edu.cn", "path": "/"}]
        client = LibraryClient(cookies, "http://" + HOST, transport=httpx.MockTransport(handle))
        self.assertTrue(client.check()["connected"])
        self.assertEqual(calls[0].method, "GET")
        self.assertEqual(calls[0].headers["cookie"], "PHPSESSID=synthetic-reader-session")

    def test_expired_or_unsafe_redirect_is_not_followed(self):
        for location in ("login.php", "https://passport.ustc.edu.cn/login", "/reader/book_lst.php?action=renew", "http://opac.lib.ustc.edu.cn:invalid/reader/book_lst.php"):
            calls = []
            def handle(request):
                calls.append(request)
                return httpx.Response(302, headers={"Location": location})
            client = LibraryClient(COOKIES, "http://" + HOST, transport=httpx.MockTransport(handle))
            with self.assertRaises(LibraryError):
                client.check()
            self.assertEqual(len(calls), 1)

    def test_mutating_paths_or_query_and_incomplete_portal_are_rejected(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text="登录或维护页面")
        client = LibraryClient(COOKIES, "http://" + HOST, transport=httpx.MockTransport(handle))
        for path in ("/reader/ajax_renew.php", "/reader/book_lst.php?renew=1", "/reader/logout.php", "/reader/redr_lost.php", "/reader/fine_pec.php", "https://evil.example/"):
            with self.assertRaises(LibraryError):
                client.get(path)
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(LibraryError, "已登录"):
            client.check()

    def test_public_directory_has_no_private_cookies_or_token_links(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text='<a href="http://opac.lib.ustc.edu.cn/reader/login.php">我的图书馆</a><a href="https://lib.ustc.edu.cn/?ticket=synthetic-private">令牌链接</a><a href="https://external.example/">数据库</a>')
        result = list_services(httpx.MockTransport(handle))
        self.assertEqual(result["count"], 2)
        self.assertNotIn("cookie", calls[0].headers)
        self.assertNotIn("synthetic-private", json.dumps(result))


class LibrarySSOTests(unittest.TestCase):
    def test_registered_cas_service_is_discovered_and_gateway_removed(self):
        calls = []
        def handle(request):
            calls.append(request)
            if request.url.scheme == "https":
                return httpx.Response(301, headers={"Location": "http://" + HOST + "/reader/login-cas.php"})
            return httpx.Response(302, headers={"Location": "https://passport.ustc.edu.cn/login?" + urlencode({"service": "http://" + HOST + "/reader/login-cas.php", "gateway": "true"})})
        client = httpx.Client(transport=httpx.MockTransport(handle))
        with patch("school_mcp.library.login.httpx.Client", return_value=client):
            url = urlsplit(find_sso_url())
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.hostname, "passport.ustc.edu.cn")
        self.assertEqual(parse_qs(url.query), {"service": ["http://" + HOST + "/reader/login-cas.php"]})
        self.assertTrue(all(r.method == "GET" and "cookie" not in r.headers for r in calls))

    def test_other_identity_or_service_destination_is_rejected(self):
        for target in ("https://evil.example/login?service=http://" + HOST + "/reader/login-cas.php", "https://passport.ustc.edu.cn/login?service=https://evil.example/callback", "http://passport.ustc.edu.cn/login?service=http://" + HOST + "/reader/login-cas.php", "https://passport.ustc.edu.cn/logout?service=http://" + HOST + "/reader/login-cas.php"):
            client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(302, headers={"Location": target})))
            with patch("school_mcp.library.login.httpx.Client", return_value=client), self.assertRaises(LibraryError):
                find_sso_url()


@unittest.skipUnless(os.name == "nt", "Windows DPAPI integration")
class LibrarySessionTests(unittest.TestCase):
    def test_session_is_encrypted_and_bound_to_current_account(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SCHOOL_MCP_LOCAL_DIR": directory}):
            save_credentials("synthetic-account", "synthetic-password")
            save_session([*COOKIES, {"name": "CASTGC", "value": "synthetic-sso-secret", "domain": "id.ustc.edu.cn"}], "http://" + HOST, "SyntheticBrowser/1", "synthetic-account")
            self.assertNotIn(b"synthetic-reader-session", (Path(directory) / "library.session.dpapi").read_bytes())
            self.assertEqual(load_session()["cookies"], COOKIES)
            save_credentials("synthetic-other-account", "synthetic-password")
            with self.assertRaisesRegex(LibraryError, "不一致"):
                load_session()

    def test_missing_corrupt_or_wrong_host_session_has_safe_error(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SCHOOL_MCP_LOCAL_DIR": directory}):
            with self.assertRaisesRegex(LibraryError, "尚未登录"):
                load_session()
            (Path(directory) / "library.session.dpapi").write_bytes(b"synthetic-corrupt-session")
            with self.assertRaisesRegex(LibraryError, "尚未登录"):
                load_session()
            with self.assertRaises(LibraryError):
                save_session(COOKIES, "http://evil.example", "SyntheticBrowser/1", None)


if __name__ == "__main__":
    unittest.main()
