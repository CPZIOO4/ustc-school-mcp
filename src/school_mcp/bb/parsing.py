from __future__ import annotations

import re
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .session import BASE_URL, BB_HOSTS, BBError


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def login_page(html: str, url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.hostname not in BB_HOSTS or parsed.path.startswith(("/nginx_auth/", "/webapps/login")):
        return True
    soup = soup_of(html)
    return soup.select_one('input[type="password"], form#loginForm') is not None


def safe_url(path: str) -> str:
    if not path or any(c in path for c in "\r\n\x00"):
        raise BBError("BB 页面地址无效。")
    url = urljoin(BASE_URL + "/", path)
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in BB_HOSTS or parsed.port not in (None, 443) or parsed.username or parsed.password:
            raise ValueError("invalid URL")
    except ValueError:
        raise BBError("只能读取中科大 BB 平台的 HTTPS 页面。") from None
    return url


def normalize_link(url: str) -> str:
    parsed = urlsplit(url)
    parameters = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if any(secret in key.lower() for secret in ("nonce", "csrf", "session", "password", "ticket", "token")):
            continue
        if key == "mode" and value == "reset":
            continue
        if key == "url" and not value:
            continue
        parameters.append((key, value))
    scheme = "https" if parsed.hostname in BB_HOSTS else parsed.scheme
    return urlunsplit((scheme, parsed.netloc, parsed.path, urlencode(parameters), ""))


def course_links(html: str) -> list[dict]:
    soup = soup_of(html)
    courses: dict[str, dict] = {}
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if href.startswith(("javascript:", "#")):
            continue
        parsed = urlsplit(normalize_link(urljoin(BASE_URL, href)))
        if parsed.hostname not in BB_HOSTS:
            continue
        query = parse_qs(parsed.query)
        identifier = query.get("course_id", [None])[0]
        if query.get("type") == ["Course"]:
            identifier = query.get("id", [identifier])[0]
        if not identifier or not re.fullmatch(r"_?\d+(?:_\d+)?", identifier):
            continue
        title = anchor.get_text(" ", strip=True)
        if not title:
            continue
        courses.setdefault(identifier, {"course_id": identifier, "title": title, "path": parsed.path + ("?" + parsed.query if parsed.query else "")})
    return list(courses.values())


def page_data(html: str, url: str, max_chars: int = 6000) -> dict:
    soup = soup_of(html)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    content = soup.select_one("#contentPanel, #content, #containerdiv, main") or soup.body or soup
    for element in soup.select("script, style, noscript, input, textarea, select"):
        element.decompose()
    text = content.get_text("\n", strip=True)
    links = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if href.lower().startswith(("javascript:", "#", "mailto:")):
            continue
        parsed = urlsplit(normalize_link(urljoin(url, href)))
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.path.startswith(("/webapps/login", "/nginx_auth")):
            continue
        label = anchor.get_text(" ", strip=True)
        if not label:
            continue
        internal = parsed.hostname in BB_HOSTS
        links.append({"text": label, "url": parsed.geturl(), "path": parsed.path + ("?" + parsed.query if parsed.query else "") if internal else None, "internal": internal, "course_menu": anchor.find_parent(id="courseMenuPalette_contents") is not None})
    return {"title": title, "url": url, "text": text[:max_chars], "text_truncated": len(text) > max_chars, "text_total_chars": len(text), "links": links[:200], "content_is_untrusted": True}
