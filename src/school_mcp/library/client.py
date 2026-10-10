from __future__ import annotations

from http.cookiejar import Cookie
from urllib.parse import urljoin, urlsplit

import httpx

from ..network import http_client
from ..service_errors import identity_redirect
from bs4 import BeautifulSoup

from .parsing import book_records, services, summary
from .session import BASE_URLS, HOST, PUBLIC_URL, LibraryError, library_cookies, load_session

READ_PATHS = {"/reader/redr_info.php", "/reader/book_lst.php", "/reader/book_hist.php"}


class LibraryClient:
    def __init__(self, cookies=None, base_url=None, user_agent=None, transport=None):
        session = load_session() if cookies is None else None
        self.cookies = session["cookies"] if session else library_cookies(cookies)
        self.base_url = session["base_url"] if session else base_url
        self.user_agent = session["user_agent"] if session else user_agent
        self.transport = transport
        if self.base_url not in BASE_URLS:
            raise LibraryError("个人图书馆地址无效。")

    def get(self, path: str) -> httpx.Response:
        if path not in READ_PATHS:
            raise LibraryError("此图书馆页面尚未纳入读取接口。")
        cookies = httpx.Cookies()
        for cookie in self.cookies:
            # Preserve Secure so a TLS-only Cookie is never sent to the legacy HTTP OPAC.
            cookies.jar.set_cookie(Cookie(0, cookie["name"], cookie["value"], None, False, cookie["domain"], True, cookie["domain"].startswith("."), cookie.get("path", "/"), True, bool(cookie.get("secure")), None, True, None, None, {}))
        try:
            with http_client("library", LibraryError, cookies=cookies, transport=self.transport, follow_redirects=False, timeout=20, headers={"User-Agent": self.user_agent or "SchoolMCP/0.1"}) as client:
                url = self.base_url + path
                for _ in range(4):
                    response = client.get(url)
                    if response.is_redirect:
                        target = urlsplit(urljoin(str(response.url), response.headers.get("location", "")))
                        origin = f"{target.scheme}://{target.hostname}"
                        if identity_redirect(target.geturl()) or (origin in BASE_URLS and target.port in (None, 80 if target.scheme == 'http' else 443) and not target.username and not target.password and target.path in {'/reader/login.php', '/reader/login-cas.php'}):
                            raise LibraryError('图书馆需要认证。', code='authentication_required')
                        if origin not in BASE_URLS or target.port not in (None, 80 if target.scheme == "http" else 443) or target.username or target.password or target.path not in READ_PATHS or target.query or target.fragment:
                            raise LibraryError("个人图书馆登录已失效或跳转超出读取范围，请调用 school_library_reconnect 后查看登录状态，再重试当前查询。")
                        url = origin + target.path
                        continue
                    if response.status_code in {401, 403}:
                        raise LibraryError("个人图书馆会话失效或没有访问权限。", code='authentication_required' if response.status_code == 401 else 'access_denied')
                    response.raise_for_status()
                    if len(response.content) > 5 * 1024 * 1024 or "text/html" not in response.headers.get("content-type", "").lower():
                        raise LibraryError("图书馆页面类型或大小超出读取范围。")
                    soup = BeautifulSoup(response.text, "html.parser")
                    if soup.select_one('input[type="password"], form[action*="login"]'):
                        raise LibraryError('图书馆需要认证。', code='authentication_required')
                    if soup.select_one('a[href*="logout.php"]') is None:
                        raise LibraryError("没有取得已登录的个人图书馆页面，请调用 school_library_reconnect 后查看登录状态，再重试当前查询。")
                    return response
                raise LibraryError("图书馆页面跳转次数超过限制。")
        except LibraryError:
            raise
        except (httpx.HTTPError, ValueError):
            raise LibraryError("无法读取个人图书馆，请检查网络与登录状态。") from None

    def check(self) -> dict:
        response = self.get("/reader/redr_info.php")
        return {"connected": True, "read_only": True, "transport": urlsplit(str(response.url)).scheme, "service": "中国科学技术大学个人图书馆"}

    def summary(self, include_card_dates: bool = False) -> dict:
        return summary(self.get("/reader/redr_info.php").text, include_card_dates)

    def loans(self, include_identifiers: bool = False) -> dict:
        return book_records(self.get("/reader/book_lst.php").text, "current_loans", include_identifiers)

    def history(self, include_identifiers: bool = False) -> dict:
        return book_records(self.get("/reader/book_hist.php").text, "loan_history", include_identifiers)


def list_services(transport=None) -> dict:
    # Public directory requests never use personal OPAC or identity-provider Cookies.
    try:
        with http_client("library-public", LibraryError, timeout=20, follow_redirects=False, transport=transport) as client:
            response = client.get(PUBLIC_URL + "/")
        response.raise_for_status()
        if response.is_redirect or len(response.content) > 5 * 1024 * 1024 or "text/html" not in response.headers.get("content-type", "").lower():
            raise LibraryError("图书馆主页类型、跳转或大小超出读取范围。")
        return services(response.text)
    except httpx.HTTPError:
        raise LibraryError("无法读取图书馆公共服务目录。") from None
