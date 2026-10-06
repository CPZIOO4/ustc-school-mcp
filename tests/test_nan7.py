from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx

from school_mcp.bb.identity import save_credentials
from school_mcp.nan7.client import Nan7Client
from school_mcp.nan7.login import is_market_origin
from school_mcp.nan7.reconnect import status
from school_mcp.nan7.session import API_URL, Nan7Error, load_session, save_session

OFFER = {"id": 123, "title": "测试图书", "type": "sell", "price": "12.50", "valid": True, "owner": {"id": 1, "name": "测试用户", "email": "not-exposed@example.com"}, "images": [{"v240": "https://img.example/test.jpg"}], "detail": "测试详情", "token": "should-not-return"}


class Nan7RequestTests(unittest.TestCase):
    def test_search_filters_cursor_and_token_scope(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, json={"offers": [OFFER], "nextPage": "opaque-cursor", "prevPage": None})
        result = Nan7Client("synthetic-token", httpx.MockTransport(handle)).search(" 图书 ", "sell", 1, "previous-cursor")
        self.assertEqual(result["next_page"], "opaque-cursor")
        self.assertEqual(result["offers"][0]["price"], "12.50")
        self.assertEqual(result["count"], 1)
        self.assertNotIn("should-not-return", json.dumps(result))
        self.assertNotIn("not-exposed", json.dumps(result))
        self.assertEqual(str(calls[0].url), API_URL + "/v1/offer/search")
        self.assertEqual(calls[0].method, "POST")
        self.assertEqual(json.loads(calls[0].content), {"type": "sell", "query": "图书", "category": 1, "page": "previous-cursor"})
        self.assertEqual(calls[0].headers["authorization"], "Bearer synthetic-token")
        self.assertNotIn("cookie", calls[0].headers)

    def test_detail_and_unavailable(self):
        client = Nan7Client("synthetic-token", httpx.MockTransport(lambda r: httpx.Response(200, json=OFFER)))
        self.assertEqual(client.detail("123")["offer"]["detail"], "测试详情")
        with self.assertRaisesRegex(Nan7Error, "不一致"):
            client.detail("124")
        client = Nan7Client("synthetic-token", httpx.MockTransport(lambda r: httpx.Response(200, json={"valid": False, "editable": False})))
        self.assertFalse(client.detail("123")["available"])

    def test_redirect_auth_failure_and_bad_response_are_not_empty_success(self):
        for response in [httpx.Response(302, headers={"location": "https://evil.example"}), httpx.Response(401, json={"message": "synthetic-secret"}), httpx.Response(403), httpx.Response(500), httpx.Response(200, text="login"), httpx.Response(200, json={}), httpx.Response(200, json={"offers": [None]})]:
            calls = []
            def handle(request):
                calls.append(request)
                return response
            with self.assertRaises(Nan7Error) as caught:
                Nan7Client("synthetic-token", httpx.MockTransport(handle)).search()
            self.assertEqual(len(calls), 1)
            self.assertNotIn("synthetic", str(caught.exception))

    def test_mutations_and_invalid_arguments_never_send_requests(self):
        calls = []
        client = Nan7Client("synthetic-token", httpx.MockTransport(lambda r: calls.append(r)))
        for path in ["/v1/offer/new", "/v1/offer/stop", "/v1/offer/update", "https://evil.example", "/v1/offer/get?edit=1"]:
            with self.assertRaises(Nan7Error):
                client._post(path, {})
        for kwargs in [{"offer_type": "new"}, {"category": 7}, {"category": True}, {"category": 1, "offer_type": "buy"}, {"query": "x" * 201}, {"page": "\n"}]:
            with self.assertRaises(Nan7Error):
                client.search(**kwargs)
        for offer_id in ["https://evil.example", "123/stop", "", "１２３"]:
            with self.assertRaises(Nan7Error):
                client.detail(offer_id)
        self.assertEqual(calls, [])

    def test_response_size_and_content_type_are_enforced(self):
        for response in [httpx.Response(200, headers={"content-type": "text/html"}, text='{"offers": []}'), httpx.Response(200, json={"offers": [], "padding": "x" * 1024})]:
            with patch("school_mcp.nan7.client.MAX_RESPONSE_BYTES", 128):
                with self.assertRaises(Nan7Error):
                    Nan7Client("synthetic-token", httpx.MockTransport(lambda r: response)).search()

    def test_hexadecimal_offer_ids_are_preserved(self):
        offer_id = "abcdef0123456789abcdef0123456789"
        client = Nan7Client("synthetic-token", httpx.MockTransport(lambda r: httpx.Response(200, json={**OFFER, "id": offer_id})))
        self.assertEqual(client.detail(offer_id)["offer"]["id"], offer_id)


class Nan7SessionTests(unittest.TestCase):
    def test_encrypted_session_account_binding_and_offline_status(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SCHOOL_MCP_LOCAL_DIR": directory}):
            save_credentials("test-account", "synthetic-password")
            save_session("synthetic-token", "test-account")
            self.assertEqual(load_session()["token"], "synthetic-token")
            with patch("httpx.Client", side_effect=AssertionError("must stay offline")):
                value = status()
            self.assertTrue(value["configured"])
            self.assertFalse(value["network_checked"])
            self.assertNotIn("synthetic", json.dumps(value))
            from pathlib import Path
            self.assertNotIn(b"synthetic-token", (Path(directory) / "nan7-session.dpapi").read_bytes())
            save_credentials("another-account", "synthetic-password")
            with self.assertRaisesRegex(Nan7Error, "账号不一致"):
                load_session()

    def test_missing_session_and_restricted_market_origin(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SCHOOL_MCP_LOCAL_DIR": directory}):
            self.assertFalse(status()["configured"])
            with self.assertRaisesRegex(Nan7Error, "school_nan7_reconnect"):
                Nan7Client()
        self.assertTrue(is_market_origin("https://nan7market.com/cas?ticket=synthetic"))
        for url in ["http://nan7market.com", "https://nan7market.com.evil.example", "https://nan7market.com:444/", "https://user@nan7market.com", "https://nan7market.com:bad/"]:
            self.assertFalse(is_market_origin(url))


if __name__ == "__main__":
    unittest.main()
