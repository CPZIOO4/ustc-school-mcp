from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from ..network import http_client
from bs4 import BeautifulSoup

BASE_URL = "https://www.teach.ustc.edu.cn"
CATEGORIES = {"notice": "通知新闻", "notice-teaching": "教学", "notice-info": "信息", "notice-exchange": "交流", "notice-exam": "考试"}
ARTICLE_PATH = re.compile(r"/(?:notice|education|service|calendar)(?:/[a-z][a-z0-9-]*)*/[1-9][0-9]*\.html")
MAX_BYTES = 5 * 1024 * 1024


class TeachError(RuntimeError):
    pass


def category_path(category: str) -> str:
    if category not in CATEGORIES:
        raise TeachError("未知栏目，请使用 list_categories 返回的 id。")
    return "/category/notice" + ("/" + category if category != "notice" else "")


def public_link(href: str, source_url: str) -> str | None:
    url = urljoin(source_url, href)
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        if re.search(r"(?:^|&)(?:ticket|token|password|auth|code|session)[^=]*=", parts.query, re.I):
            return None
        return url
    except ValueError:
        return None


def article_url(value: str) -> str:
    try:
        parts = urlsplit(urljoin(BASE_URL, value))
        if (parts.scheme != "https" or parts.hostname != "www.teach.ustc.edu.cn"
                or parts.port not in (None, 443) or parts.username or parts.password
                or parts.query or parts.fragment or not ARTICLE_PATH.fullmatch(parts.path)):
            raise ValueError
        return BASE_URL + parts.path
    except ValueError:
        raise TeachError("仅支持本站通知、教育、事务及教学日历的数字编号 .html 正文链接；站外链接和附件仅供查看来源。") from None


def restricted(soup: BeautifulSoup, url: str) -> dict[str, Any] | None:
    heading = soup.find("h1")
    if heading and "受限资源" in heading.get_text():
        return {"access": "restricted", "source_url": url, "message": "网站要求校内网络或统一认证；当前适配器仅匿名读取，未登录，也不将此页面视为空结果。"}
    return None


def _text(node) -> str | None:
    return node.get_text(" ", strip=True) if node else None


def parse_listing(html: str, url: str, page: int, category: str | None = None) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    if denied := restricted(soup, url):
        return denied
    main = soup.select_one("main")
    if main is None or main.select_one(".list") is None:
        raise TeachError("未识别到通知或搜索列表，网站结构可能改变。")
    records = []
    for row in main.select(".article-list > li"):
        link = row.select_one(".post > a[href]")
        target = public_link(link["href"], url) if link else None
        if not target:
            continue
        tags = [{"name": _text(a), "source_url": public_link(a["href"], url)} for a in row.select(".cat-tags a[href]")]
        if not tags and category and category != "notice":
            tags = [{"name": CATEGORIES[category], "source_url": BASE_URL + category_path(category)}]
        try:
            article_url(target)
            readable = True
        except TeachError:
            readable = False
        records.append({"title": _text(link), "source_url": target, "published_at": _text(row.select_one(".date")), "categories": tags, "pinned": "sticky" in row.get("class", []), "readable_by_adapter": readable})
    if not records and "未找到相关文章" not in main.get_text():
        raise TeachError("页面没有可识别的条目或明确的无结果提示，不能判定为空。")
    current = main.select_one(".pagination .current")
    actual_page = int(current.get_text(strip=True)) if current and current.get_text(strip=True).isdigit() else page
    if actual_page != page:
        raise TeachError("站点返回的页码与请求不一致，未将重复首页当作新一页。")
    pagination = []
    for a in main.select(".pagination a[href]"):
        target = public_link(a["href"], url)
        if target and urlsplit(target).hostname == "www.teach.ustc.edu.cn":
            pagination.append({"label": _text(a), "source_url": target})
    next_link = main.select_one(".pagination a.next[href]")
    prev_link = main.select_one(".pagination a.prev[href]")
    return {"access": "public", "source_url": url, "title": _text(soup.find("h1")), "page": actual_page,
            "count": len(records), "records": records,
            "next_page_url": public_link(next_link["href"], url) if next_link else None,
            "previous_page_url": public_link(prev_link["href"], url) if prev_link else None,
            "pagination_links": pagination, "scope": "仅本页；置顶条目可能跨页重复，站点搜索匹配规则由网站决定。"}


def parse_article(html: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    if denied := restricted(soup, url):
        return denied
    body = soup.select_one("main article")
    title = soup.select_one(".single-title h1")
    if body is None or title is None:
        raise TeachError("未识别到文章正文，网站结构可能改变或此页面不是正文。")
    for node in body.select("script, style, form, input, button, iframe, noscript"):
        node.decompose()
    links = []
    for a in body.select("a[href]"):
        if target := public_link(a["href"], url):
            links.append({"title": _text(a), "source_url": target, "attachment": a.find_parent(class_="attachments") is not None})
    images = [{"alt": img.get("alt", ""), "source_url": target} for img in body.select("img[src]") if (target := public_link(img["src"], url))]
    return {"access": "public", "source_url": url, "title": _text(title),
            "published_at": _text(soup.select_one(".meta-date")), "modified_at": _text(soup.select_one(".meta-last-modified")),
            "categories": [_text(a) for a in soup.select(".meta-categories a")],
            "text": body.get_text("\n", strip=True), "links": links, "images": images,
            "attachments_downloaded": False, "content_is_untrusted": True}


class TeachClient:
    def __init__(self, transport=None):
        self.transport = transport

    def _get(self, url: str) -> tuple[str, str]:
        # URLs are constructed by the narrow public methods. No personal cookies or SSO state.
        try:
            with http_client("teach", TeachError, timeout=25, transport=self.transport, follow_redirects=False, headers={"User-Agent": "SchoolMCP/0.1"}) as client:
                with client.stream("GET", url) as response:
                    if response.is_redirect:
                        raise TeachError("教务站返回跳转；匿名读取未跟随，可能需要统一认证或链接已变更。")
                    if response.status_code in {401, 403}:
                        raise TeachError("教务站限制访问；当前仅支持匿名读取。")
                    if response.status_code == 404:
                        raise TeachError("教务站未找到此文章或页码。")
                    response.raise_for_status()
                    if "text/html" not in response.headers.get("content-type", "").lower():
                        raise TeachError("返回内容不是 HTML 页面。")
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise TeachError("页面超过读取大小限制。")
                        chunks.append(chunk)
                    return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace"), str(response.url)
        except httpx.HTTPError:
            raise TeachError("无法读取教务站，请检查网络或稍后重试。") from None

    @staticmethod
    def _page(page: int) -> str:
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 10000:
            raise TeachError("page 必须为 1 至 10000 的整数。")
        return f"/page/{page}" if page > 1 else ""

    def list_notices(self, category: str = "notice", page: int = 1) -> dict[str, Any]:
        html, url = self._get(BASE_URL + category_path(category) + self._page(page))
        return parse_listing(html, url, page, category)

    def search(self, keyword: str, page: int = 1) -> dict[str, Any]:
        keyword = keyword.strip()
        if not keyword or len(keyword) > 100 or any(ord(c) < 32 for c in keyword):
            raise TeachError("keyword 必须为 1 至 100 个字符，不能含控制字符。")
        html, url = self._get(BASE_URL + "/search/" + quote(keyword, safe="") + self._page(page))
        result = parse_listing(html, url, page)
        result["keyword"] = keyword
        return result

    def read_article(self, url: str) -> dict[str, Any]:
        html, source_url = self._get(article_url(url))
        return parse_article(html, source_url)

    def check(self) -> dict[str, Any]:
        result = self.list_notices()
        return {"connected": result["access"] == "public", "access": result["access"], "source_url": result["source_url"], "read_only": True, "count_on_first_page": result.get("count")}
