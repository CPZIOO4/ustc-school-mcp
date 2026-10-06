from __future__ import annotations

from network_test_support import isolate_pacing as setUpModule

import asyncio
import unittest
from unittest.mock import patch

import httpx

from school_mcp.teach.client import BASE_URL, TeachClient, TeachError, parse_article, parse_listing
from school_mcp.teach.server import mcp, school_teach_status

LIST = '''<h1>通知新闻</h1><main><div class="list"><ul class="article-list">
<li class="sticky"><span class="post"><a href="/notice/notice-info/123.html">测试通知</a></span><span class="date">2026-01-01</span></li>
<li><span class="post"><a href="https://news.ustc.edu.cn/info/123.htm">站外新闻</a></span></li>
</ul></div><div class="pagination"><span class="current">1</span><a class="next" href="/category/notice/page/2">下一页</a></div></main>'''
ARTICLE = '''<div class="single-title"><h1>测试正文</h1></div><main><article>
<p>测试内容</p><script>hidden-secret</script><form>private-token</form>
<aside class="attachments"><a href="/?attachment_id=123">测试附件</a><a href="javascript:history.go(-1)">返回</a></aside>
<a href="https://other.example/?token=secret">不得导出</a><img src="/wp-content/test.png" alt="图片内容">
</article></main><div class="meta-date">2026-01-01 12:00</div><div class="meta-last-modified">2026-01-02 12:00</div>'''


class TeachParsingTests(unittest.TestCase):
    def test_listing_preserves_pinned_external_and_pagination(self):
        result = parse_listing(LIST, BASE_URL + "/category/notice", 1)
        self.assertEqual(result["count"], 2)
        self.assertTrue(result["records"][0]["pinned"])
        self.assertTrue(result["records"][0]["readable_by_adapter"])
        self.assertFalse(result["records"][1]["readable_by_adapter"])
        self.assertEqual(result["next_page_url"], BASE_URL + "/category/notice/page/2")

    def test_empty_restricted_and_unknown_are_distinct(self):
        result = parse_listing('<h1>搜索结果</h1><main><div class="list">抱歉，未找到相关文章。</div></main>', BASE_URL, 1)
        self.assertEqual(result["count"], 0)
        for parser in (lambda h: parse_listing(h, BASE_URL, 1), lambda h: parse_article(h, BASE_URL)):
            denied = parser('<h1>受限资源</h1><p>请先统一认证登录</p>')
            self.assertEqual(denied["access"], "restricted")
            self.assertNotIn("records", denied)
            with self.assertRaises(TeachError):
                parser('<h1>维护</h1><main><div class="list">稍后再试</div></main>')

    def test_article_extracts_content_and_links_without_controls(self):
        result = parse_article(ARTICLE, BASE_URL + "/notice/notice-info/123.html")
        self.assertEqual(result["published_at"], "2026-01-01 12:00")
        self.assertEqual(len(result["links"]), 1)
        self.assertTrue(result["links"][0]["attachment"])
        self.assertEqual(len(result["images"]), 1)
        self.assertNotIn("hidden-secret", result["text"])
        self.assertNotIn("private-token", result["text"])

    def test_page_mismatch_is_not_silently_returned(self):
        with self.assertRaisesRegex(TeachError, "页码"):
            parse_listing(LIST, BASE_URL + "/category/notice/page/2", 2)


class TeachRequestTests(unittest.TestCase):
    def client(self, handler):
        return TeachClient(transport=httpx.MockTransport(handler))

    def test_search_encodes_keyword_and_has_no_private_headers(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, headers={"Content-Type": "text/html; charset=utf-8"}, text=LIST.replace('class="current">1', 'class="current">2'))
        result = self.client(handle).search("保研 & 教学", 2)
        self.assertEqual(result["page"], 2)
        self.assertEqual(calls[0].method, "GET")
        self.assertEqual(calls[0].url.path, "/search/保研 & 教学/page/2")
        self.assertNotIn("cookie", calls[0].headers)
        self.assertNotIn("authorization", calls[0].headers)

    def test_rejects_unsafe_and_mutating_urls_before_network(self):
        def handle(request):
            self.fail("unexpected network")
        client = self.client(handle)
        for url in ("https://evil.example/notice/notice-info/123.html", "http://www.teach.ustc.edu.cn/notice/123.html", "/wp-admin/post.php", "/?attachment_id=12", "/notice/123.html?action=delete", "https://www.teach.ustc.edu.cn:bad/notice/123.html", "/notice/%2e%2e/123.html"):
            with self.assertRaises(TeachError):
                client.read_article(url)
        for page in (0, -1, 10001, True):
            with self.assertRaises(TeachError):
                client.list_notices(page=page)
        with self.assertRaises(TeachError):
            client.list_notices(category="../wp-admin")
        with self.assertRaises(TeachError):
            client.search(" ")

    def test_redirect_is_not_followed(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(302, headers={"Location": "https://id.ustc.edu.cn/cas/login"})
        with self.assertRaisesRegex(TeachError, "跳转"):
            self.client(handle).check()
        self.assertEqual(len(calls), 1)

    def test_http_errors_and_non_html_are_not_empty_success(self):
        for status, content_type in ((403, "text/html"), (404, "text/html"), (500, "text/html"), (200, "application/pdf")):
            with self.subTest(status=status, content_type=content_type):
                with self.assertRaises(TeachError):
                    self.client(lambda r: httpx.Response(status, headers={"Content-Type": content_type}, text=LIST)).check()

    def test_oversize_response_is_rejected(self):
        with patch("school_mcp.teach.client.MAX_BYTES", 10):
            with self.assertRaisesRegex(TeachError, "大小"):
                self.client(lambda r: httpx.Response(200, headers={"Content-Type": "text/html"}, text=LIST)).check()

    def test_status_is_offline_and_tools_provide_structured_schema(self):
        with patch("httpx.Client", side_effect=AssertionError("network")):
            self.assertFalse(school_teach_status()["network_checked"])
        tools = asyncio.run(mcp.list_tools())
        self.assertEqual(len(tools), 6)
        for tool in tools:
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertIsNotNone(tool.outputSchema)


if __name__ == "__main__":
    unittest.main()
