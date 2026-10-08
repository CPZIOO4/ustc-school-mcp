"""Inspect the observed Original assignment view without executing page scripts."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from .. import network
from ..browser import chrome_browser
from .assignments import ASSIGNMENT_PATH, ID, catalog
from .parsing import login_page, safe_url, soup_of
from .session import BBError, load_session


def review_url(entry: dict, course_id: str, content_id: str) -> str:
    """Accept only the exact view shape observed on this installation."""
    url = safe_url(entry['entry_path'])
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.path != ASSIGNMENT_PATH or parsed.fragment
            or set(query) - {'course_id', 'content_id', 'mode', 'group_id'}
            or query.get('course_id') != [course_id]
            or query.get('content_id') != [content_id]
            or query.get('mode') != ['view']
            or query.get('group_id', ['']) != ['']):
        raise BBError('此作业入口不是已验证的个人作业查看模式，已停止；不进入新尝试或小组操作。')
    return url


def parse_review(html: str, *, include_attachment_names: bool = False) -> dict:
    soup = soup_of(html)
    heading = soup.select_one('#pageTitleHeader')
    title = heading.get_text(' ', strip=True) if heading else ''
    history = title.startswith(('复查提交历史记录', 'Review Submission History'))
    result = {'page_kind': 'submission_history' if history else 'unverified_page',
              'submission_state': 'unknown', 'catalog_complete': False,
              'new_attempt_requested': False, 'uploaded_by_this_call': False, 'submitted_by_this_call': False,
              'upload_supported': False, 'content_is_untrusted': True}
    if not history:
        result['next_action'] = 'verify_page_template_before_proceeding'
        result['note'] = '页面不是已验证的历史记录模板；没有运行脚本或操作表单，不能推断未提交。'
        return result
    info = soup.select_one('#assignmentInfo')
    due = []
    if info:
        for label in info.select('h3'):
            if label.get_text(' ', strip=True) in {'截止日期', 'Due Date'}:
                value = label.find_next_sibling('p')
                if value:
                    due.append(value.get_text(' ', strip=True)[:300])
    attempt = soup.select_one('#currentAttempt_label')
    date = attempt.select_one('.dateStamp') if attempt else None
    label = attempt.select_one('.mainLabel') if attempt else None
    label_text = label.get_text(' ', strip=True) if label else ''
    attachments = soup.select('#currentAttempt_submissionList a.attachment')
    # Presence of a history page/attachment alone is not a new submission receipt.
    result.update({'submission_state': 'historical_attempt_present' if attempt else 'unknown',
                   'due_text': due, 'due_text_available': bool(due),
                   'attempt_scope': 'currently_displayed_attempt',
                   'attempt_time_text': date.get_text(' ', strip=True)[:200] if date else None,
                   'late_label_present': bool(re.search(r'逾期|迟交|\blate\b', label_text, re.I)),
                   'attachment_count': len(attachments), 'attachment_names_omitted': not include_attachment_names,
                   'new_attempt_control_present': False, 'continue_control_present': False})
    for control in soup.select('#bottom_submitButtonRow input, #bottom_submitButtonRow button, #bottom_submitButtonRow a'):
        text = control.get('value', '') or control.get_text(' ', strip=True)
        if text.strip() in {'开始新的', 'Start New'}:
            result['new_attempt_control_present'] = True
        if text.strip() in {'继续', 'Continue'}:
            result['continue_control_present'] = True
    if include_attachment_names:
        result['attachment_names'] = [node.get_text(' ', strip=True)[:300] for node in attachments[:50]]
        result['attachment_names_truncated'] = len(attachments) > 50
    result['next_action'] = 'stop_at_existing_history'
    result['note'] = '仅核对当前显示的历史尝试；不是全部历史、不是本次提交回执。存在开始新的按钮不代表目前可重交；未标迟交也不代表按时。'
    return result


def review_request_allowed(url: str, method: str, target: str, *, navigation: bool, main_frame: bool) -> bool:
    return method == 'GET' and url == target and navigation and main_frame


def inspect_assignment(course_id: str, content_id: str, *, include_attachment_names=False) -> dict:
    if not ID.fullmatch(course_id) or not ID.fullmatch(content_id):
        raise BBError('请使用课程与作业查询返回的有效ID。')
    listing = catalog(course_id, max_pages=15, limit=50)
    matches = [entry for entry in listing['assignments'] if entry['content_id'] == content_id]
    if len(matches) != 1:
        raise BBError('未在当前课程内容页核实该作业，请重新查找，不猜测入口。')
    target = review_url(matches[0], course_id, content_id)
    blocked = set()
    try:
        network.limiter.acquire('bb')
        with sync_playwright() as playwright, chrome_browser(playwright, headless=True) as browser:
            context = browser.new_context(locale='zh-CN', java_script_enabled=False,
                                          service_workers='block', accept_downloads=False)
            context.add_cookies(load_session()['cookies'])
            page = context.new_page()
            def guard(route):
                request = route.request
                if review_request_allowed(request.url, request.method, target,
                                          navigation=request.is_navigation_request(),
                                          main_frame=request.frame == page.main_frame):
                    route.continue_()
                else:
                    blocked.add((request.method, urlsplit(request.url).path))
                    route.abort()
            context.route('**/*', guard)
            response = page.goto(target, wait_until='domcontentloaded', timeout=25000)
            if response is None:
                raise BBError('作业查看未收到页面响应。')
            network.limiter.response('bb', response.status, response.headers)
            if response.status >= 400:
                raise BBError('作业查看失败或访问受限，请检查登录状态和课程权限。')
            html = page.content()
            if len(html.encode('utf-8')) > 5 * 1024 * 1024:
                raise BBError('作业查看页面超过大小限制。')
            if login_page(html, page.url):
                network.limiter.failure('bb', authentication=True)
                raise BBError('BB 登录已失效，请恢复登录后重试。')
            result = parse_review(html, include_attachment_names=include_attachment_names)
            network.limiter.success('bb')
            return {'course_id': course_id, 'content_id': content_id, 'title': matches[0]['title'],
                    **result, 'headless': True, 'page_scripts_executed': False,
                    'blocked_request_count': len(blocked)}
    except network.PolicyError as exc:
        raise BBError(str(exc)) from None
    except PlaywrightError:
        try:
            network.limiter.failure('bb')
        except network.PolicyError:
            pass
        raise BBError('后台作业查看失败或遇到未适配跳转，已停止；没有上传、保存或提交。') from None
