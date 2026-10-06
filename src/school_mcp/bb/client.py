from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from .parsing import course_links, login_page, page_data, safe_url
from .session import BASE_URL, BB_HOSTS, PORTAL_PATH, BBError, load_session

READ_PATHS = (
    "/webapps/portal/execute/tabs/tabAction",
    "/webapps/blackboard/execute/launcher",
    "/webapps/blackboard/content/listContent.jsp",
    "/webapps/blackboard/content/launchLink.jsp",
    "/webapps/blackboard/execute/announcement",
    "/webapps/blackboard/execute/courseMain",
    "/webapps/blackboard/execute/modulepage/view",
    "/webapps/blackboard/execute/displayLearningUnit",
    "/webapps/blackboard/execute/content/blankPage",
)


def validate_read_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.path not in READ_PATHS:
        raise BBError("此页面尚未纳入读取接口；请从课程或页面结果中选择支持的只读页面。")
    allowed_keys = {"course_id", "id", "type", "mode", "content_id", "toc_id", "tool_id", "tool_type", "tab_tab_group_id", "tab_id", "tabId", "courseTocLabel", "cmp_tab_id", "method", "action", "context", "displayMode", "editMode", "navItem", "url", "viewChoice", "handle"}
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key, values in query.items():
        if key not in allowed_keys or any(len(value) > 1000 or any(c in value for c in "\r\n\x00") for value in values):
            raise BBError("此页面参数尚未纳入只读接口。")
        if key in {"mode", "method", "action", "editMode", "displayMode"}:
            allowed_values = {"view", "list", "listAnnouncements", "display", "false", "false_", "read", "Full", ""}
            if key == "method" and parsed.path == "/webapps/blackboard/execute/announcement":
                allowed_values.add("search")
            if any(value not in allowed_values for value in values):
                raise BBError("此页面包含不支持的操作参数。")
        if key == "type" and any(value != "Course" for value in values):
            raise BBError("此接口只允许课程入口。")
        if key == "url" and any(value for value in values):
            raise BBError("课程入口不允许自定义跳转地址。")
        if key == "tool_type" and any(value != "TOOL" for value in values):
            raise BBError("此课程菜单类型暂不支持。")


class BBClient:
    def __init__(self, cookies: list[dict] | None = None, transport: httpx.BaseTransport | None = None):
        self.cookies = cookies if cookies is not None else load_session()["cookies"]
        self.transport = transport

    def get(self, path: str) -> httpx.Response:
        url = safe_url(path)
        validate_read_url(url)
        cookie_jar = httpx.Cookies()
        for cookie in self.cookies:
            cookie_jar.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie.get("path", "/"))
        try:
            with httpx.Client(cookies=cookie_jar, follow_redirects=False, timeout=20, transport=self.transport, headers={"User-Agent": "SchoolMCP/0.1 (personal course reader)"}) as client:
                for _ in range(8):
                    response = client.get(url)
                    if response.is_redirect:
                        destination = urljoin(str(response.url), response.headers.get("location", ""))
                        target = urlsplit(destination)
                        if target.hostname not in BB_HOSTS or target.path.startswith(("/webapps/login", "/nginx_auth")):
                            raise BBError("BB 登录会话已失效，请调用 school_bb_reconnect 后查看登录状态，再重试当前查询。")
                        url = safe_url(destination)
                        validate_read_url(url)
                        continue
                    if response.status_code in {401, 403} or login_page(response.text, str(response.url)):
                        raise BBError("BB 登录会话已失效或没有访问权限，请调用 school_bb_reconnect 后查看登录状态，再重试当前查询。")
                    response.raise_for_status()
                    if "text/html" not in response.headers.get("content-type", "").lower():
                        raise BBError("返回内容不是 BB 页面。")
                    if len(response.content) > 5 * 1024 * 1024:
                        raise BBError("此 BB 页面超过读取大小限制。")
                    return response
                raise BBError("BB 页面跳转次数超过限制。")
        except BBError:
            raise
        except httpx.TimeoutException:
            raise BBError("连接 BB 平台超时，请稍后重试。") from None
        except httpx.HTTPError:
            raise BBError("无法读取 BB 页面，请检查网络和访问权限。") from None

    def check(self) -> dict:
        response = self.get(PORTAL_PATH)
        courses = course_links(response.text)
        title = page_data(response.text, str(response.url), max_chars=1000)["title"]
        return {"connected": True, "title": title, "course_links_in_portal": len(courses), "read_only": True}

    def courses(self, keyword: str = "", term: str = "") -> dict:
        if len(keyword) > 200 or (term and not re.fullmatch(r"20\d{2}(?:FA|SP|SU|WI)", term)):
            raise BBError("课程关键字过长，或学期格式无效；学期示例为 2026FA。")
        response = self.get(PORTAL_PATH)
        courses = course_links(response.text)
        filtered = [course for course in courses if keyword.casefold() in course["title"].casefold() and (not term or term in course["title"])]
        return {"courses": filtered, "count": len(filtered), "total_in_portal": len(courses), "source": str(response.url), "content_is_untrusted": True, "note": "仅返回门户 HTML 中提供的课程；若门户含动态课程模块，需要单独适配。" if not courses else ""}

    def read_page(self, path: str = PORTAL_PATH, max_chars: int = 30000) -> dict:
        if not 1 <= max_chars <= 100000:
            raise BBError("max_chars 范围为 1–100000。")
        response = self.get(path)
        return page_data(response.text, str(response.url), max_chars=max_chars)

    def course_page(self, course_id: str, max_chars: int = 30000) -> dict:
        if not re.fullmatch(r"_?\d+(?:_\d+)?", course_id):
            raise BBError("请使用课程列表中的有效 course_id。")
        return self.read_page(f"/webapps/blackboard/execute/launcher?type=Course&id={course_id}", max_chars=max_chars)

    def announcements(self, course_id: str, max_chars: int = 30000) -> dict:
        if not re.fullmatch(r"_?\d+(?:_\d+)?", course_id):
            raise BBError("请使用课程列表中的有效 course_id。")
        return self.read_page(f"/webapps/blackboard/execute/announcement?method=search&context=course_entry&course_id={course_id}&handle=announcements_entry&mode=view", max_chars=max_chars)
