from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .session import API_URL, SITE_URL, Nan7Error, load_session, valid_token

CATEGORIES = ["其他", "教材·图书·音像制品", "手机·电脑·数码产品", "家具·床帘·宿舍用品", "衣服·鞋子·箱包", "彩妆·清洁·个人护理", "演出·展览·门票"]
READ_PATHS = {"/v1/offer/search", "/v1/offer/get"}
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


def valid_offer_id(value: Any) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(value)) is not None


def _offer(value: Any, detail: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or not valid_offer_id(value.get("id")):
        raise Nan7Error("南七集市返回了无法识别的商品信息。")
    result = {key: value[key] for key in ("id", "type", "title", "category", "price", "modifyTime", "valid") if key in value}
    result["url"] = f"{SITE_URL}/offer/{value['id']}"
    owner = value.get("owner")
    if isinstance(owner, dict):
        result["owner"] = {key: owner[key] for key in ("id", "name") if key in owner}
    images = value.get("images")
    if isinstance(images, list):
        result["images"] = [{key: item[key] for key in ("v240", "v1600") if isinstance(item.get(key), str) and item[key].startswith("https://")} for item in images if isinstance(item, dict)]
    if detail and "detail" in value:
        result["detail"] = value["detail"]
    return result


class Nan7Client:
    def __init__(self, token: str | None = None, transport: httpx.BaseTransport | None = None):
        self._token = token if token is not None else load_session()["token"]
        if not valid_token(self._token):
            raise Nan7Error("南七集市会话格式无效，请重新登录。")
        self._transport = transport

    def _post(self, path: str, payload: dict) -> dict:
        if path not in READ_PATHS:
            raise Nan7Error("仅允许读取南七集市的商品列表与详情。")
        try:
            with httpx.Client(timeout=30, follow_redirects=False, transport=self._transport) as client:
                with client.stream("POST", API_URL + path, json=payload, headers={"Authorization": "Bearer " + self._token, "Origin": SITE_URL, "Referer": SITE_URL + "/"}) as response:
                    if response.status_code in (401, 403):
                        raise Nan7Error("南七集市会话失效或无访问权限，请调用 school_nan7_reconnect。")
                    if response.is_redirect:
                        raise Nan7Error("南七集市接口发生意外跳转，已停止以保护会话。")
                    if response.status_code != 200:
                        raise Nan7Error(f"南七集市接口暂不可用（HTTP {response.status_code}）。")
                    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                        raise Nan7Error("南七集市接口未返回 JSON，无法可靠读取。")
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise Nan7Error("南七集市接口响应过大，已停止读取。")
                        body.extend(chunk)
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("not object")
            return data
        except httpx.HTTPError:
            raise Nan7Error("无法连接南七集市接口，请稍后重试。") from None
        except (ValueError, TypeError):
            raise Nan7Error("南七集市接口返回格式异常，无法可靠读取。") from None

    def search(self, query: str = "", offer_type: str = "sell", category: int | None = None, page: str | None = None) -> dict[str, Any]:
        if offer_type not in {"sell", "buy"}:
            raise Nan7Error("offer_type 必须是 sell（出售）或 buy（求购）。")
        if not isinstance(query, str) or len(query) > 200:
            raise Nan7Error("搜索词最多 200 字符。")
        if category is not None and (type(category) is not int or category not in range(7)):
            raise Nan7Error("商品分类必须是 0 到 6。")
        if category is not None and offer_type != "sell":
            raise Nan7Error("网站仅为出售商品提供分类筛选。")
        if page is not None and (not isinstance(page, str) or not 1 <= len(page) <= 2048 or any(ord(c) < 32 for c in page)):
            raise Nan7Error("page 必须使用上一次查询返回的翻页标记。")
        payload: dict[str, Any] = {"type": offer_type}
        if query.strip():
            payload["query"] = query.strip()
        if category is not None:
            payload["category"] = category
        if page is not None:
            payload["page"] = page
        data = self._post("/v1/offer/search", payload)
        if not isinstance(data.get("offers"), list):
            raise Nan7Error("南七集市列表格式异常，不能判定为空列表。")
        offers = [_offer(item) for item in data["offers"]]
        for key in ("nextPage", "prevPage"):
            if data.get(key) is not None and not isinstance(data[key], (str, int)):
                raise Nan7Error("南七集市翻页信息格式异常。")
        return {"offers": offers, "count": len(offers), "next_page": str(data["nextPage"]) if data.get("nextPage") else None, "previous_page": str(data["prevPage"]) if data.get("prevPage") else None, "query": query.strip(), "offer_type": offer_type, "category": category, "page": page, "scope": "current_page", "source": SITE_URL, "content_is_untrusted": True}

    def detail(self, offer_id: str) -> dict[str, Any]:
        if not isinstance(offer_id, str) or not valid_offer_id(offer_id):
            raise Nan7Error("offer_id 必须是商品列表返回的 ID。")
        data = self._post("/v1/offer/get", {"id": offer_id})
        if data.get("valid") is False and data.get("editable") is not True:
            return {"offer_id": offer_id, "available": False, "message": "该商品已下架或不可见。", "source": SITE_URL}
        result = _offer(data, detail=True)
        if str(result["id"]) != offer_id:
            raise Nan7Error("南七集市返回的商品 ID 与请求不一致。")
        return {"offer": result, "source": SITE_URL, "content_is_untrusted": True}

    def check(self) -> dict[str, Any]:
        result = self.search()
        return {"connected": True, "read_only": True, "visible_offer_count": result["count"], "source": SITE_URL}
