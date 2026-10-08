"""Headless Chrome driver for the observed individual assignment workflow."""
from __future__ import annotations

import base64
import html as html_lib
from email.parser import BytesParser
from email.policy import default
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from .. import network
from ..browser import chrome_browser
from .assignment_review import review_url
from .parsing import login_page, soup_of
from .session import BBError, load_session
from .submission_contracts import form_contract, require, snapshot, validate_download


def check_multipart(body, content_type, plan):
    # CDP omits multipart file bytes. File bytes are verified in the browser's
    # File objects before arming; forward the native request unchanged.
    message = BytesParser(policy=default).parsebytes(
        ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body)
    require(message.is_multipart(), '提交请求不是预期的附件表单。')
    fields, files = {}, []
    for part in message.iter_parts():
        key = part.get_param('name', header='content-disposition')
        data = part.get_payload(decode=True) or b''
        if part.get_filename():
            files.append(part.get_filename())
        else:
            fields.setdefault(key, []).append(data.decode('utf-8'))
    a = plan['assignment']
    require(fields.get('course_id') == [a['course_id']] and fields.get('content_id') == [a['content_id']])
    require(fields.get('dispatch') == ['submit'] and fields.get('attempt_id') == ['']
            and fields.get('remove_file_id') == [''])
    require(files == [f['filename'] for f in plan['files']], '待传附件与准备记录不一致。')
    for key, values in fields.items():
        require(not ('group' in (key or '').lower() and any(values)), '小组作业不支持。')
    require(not soup_of(' '.join(fields.get('studentSubmission.text', []))).get_text(strip=True), '出现未授权的正文内容。')
    comment = soup_of(' '.join(fields.get('student_commentstext', []))).get_text('\n').strip()
    require(comment == plan['comment'].strip(), '实际附言与准备记录不一致。')


def static_request(url, method):
    p = urlsplit(url)
    return (method == 'GET' and p.scheme == 'https' and p.hostname == 'www.bb.ustc.edu.cn'
            and p.port in (None, 443) and not p.username and not p.password
            and p.path.startswith(('/javascript/', '/groupjs/', '/webapps/', '/common/', '/themes/', '/images/', '/branding/'))
            and p.path.endswith(('.js', '.css', '.png', '.gif', '.jpg', '.svg', '.woff', '.woff2', '.ico')))


class SubmissionBrowser:
    def __init__(self, context_hook=None):
        self.context_hook = context_hook  # Test-only transport injection, never a tool argument.

    def __enter__(self):
        self.playwright = sync_playwright().start()
        try:
            self.manager = chrome_browser(self.playwright, headless=True)
            self.browser = self.manager.__enter__()
            self.context = self.browser.new_context(locale='zh-CN', java_script_enabled=False,
                                                   service_workers='block', accept_downloads=False)
            self.context.add_cookies(load_session()['cookies'])
            if self.context_hook:
                self.context_hook(self.context)
            self.page = self.context.new_page()
            self.read_target = None
            def guard(route):
                r = route.request
                if r.method == 'GET' and r.url == self.read_target and r.is_navigation_request() and r.frame == self.page.main_frame:
                    route.fallback()
                else:
                    route.abort()
            self.context.route('**/*', guard)
            return self
        except Exception:
            self.playwright.stop()
            raise

    def __exit__(self, *exc):
        try:
            self.manager.__exit__(*exc)
        finally:
            self.playwright.stop()

    def read(self, target):
        network.limiter.acquire('bb')
        self.read_target = target
        response = self.page.goto(target, wait_until='domcontentloaded', timeout=25000)
        require(response is not None)
        network.limiter.response('bb', response.status, response.headers)
        require(response.ok, 'BB 页面访问失败。')
        content = self.page.content()
        require(len(content.encode()) <= 5 * 1024 * 1024)
        if login_page(content, self.page.url):
            network.limiter.failure('bb', authentication=True)
            raise BBError('BB 登录失效，请恢复登录后重新准备。')
        network.limiter.success('bb')
        return content

    def view(self, assignment):
        target = review_url(assignment, assignment['course_id'], assignment['content_id'])
        content = self.read(target)
        return snapshot(content, assignment), content, target

    def download(self, url, assignment, attempt_id):
        target, _ = validate_download(url, assignment, attempt_id)
        network.limiter.acquire('bb')
        response = self.context.request.get(target, max_redirects=0, timeout=30000)
        network.limiter.response('bb', response.status, response.headers)
        require(response.status == 200, '作业附件下载失败；不跟随未知跳转。')
        require('text/html' not in response.headers.get('content-type', '').lower(), '附件返回了网页，已停止。')
        body = response.body()
        require(0 < len(body) <= 50 * 1024 * 1024, '附件超出本机50MiB限制或为空。')
        network.limiter.success('bb')
        return body

    def send(self, plan, current, content, target, advance):
        if current['kind'] == 'history':
            require(current['new_url'], '页面不提供重新提交入口。')
            advance('new_attempt_requested')
            target = current['new_url']
            content = self.read(target)
        post_url = form_contract(content, plan['assignment'])
        state = {'rendered': False, 'armed': False, 'posted': False, 'error': None, 'dialog': False}
        context = self.browser.new_context(locale='zh-CN', storage_state=self.context.storage_state(),
                                           service_workers='block', accept_downloads=False)
        if self.context_hook:
            self.context_hook(context)
        page = context.new_page()
        def dialog(d):
            state['dialog'] = True
            d.dismiss()
        page.on('dialog', dialog)
        def guard(route):
            r = route.request
            try:
                if not state['rendered'] and r.url == target and r.method == 'GET' and r.is_navigation_request() and r.frame == page.main_frame:
                    state['rendered'] = True
                    route.fulfill(status=200, content_type='text/html; charset=utf-8', body=content)
                elif static_request(r.url, r.method) and not r.is_navigation_request():
                    route.fallback()
                elif state['armed'] and not state['posted'] and r.method == 'POST' and r.url == post_url:
                    check_multipart(r.post_data_buffer or b'', r.headers.get('content-type', ''), plan)
                    network.limiter.acquire('bb')
                    advance('submit_requested')
                    state['posted'] = True
                    route.fallback()  # Preserve actual browser file payloads.
                else:
                    route.abort()
            except Exception:
                state['error'] = '提交前校验或请求记录失败，已停止，不能自动重试。'
                route.abort()
        context.route('**/*', guard)
        try:
            page.goto(target, wait_until='load', timeout=40000)
            page.wait_for_function("window.newFile_FilePickerObject && typeof window.checkDupeFile === 'function'", timeout=15000)
            page.locator('#newFile_chooseLocalFile').set_input_files([
                {'name': f['filename'], 'mimeType': 'application/octet-stream', 'buffer': base64.b64decode(f['data'])}
                for f in plan['files']])
            comment = '<p>' + html_lib.escape(plan['comment']).replace('\n', '<br>') + '</p>' if plan['comment'] else ''
            page.evaluate("""value => { const e=window.tinyMCE && tinyMCE.get('student_commentstext');
                if(e) e.setContent(value); document.getElementById('student_commentstext').value=value; }""", comment)
            actual = page.evaluate("""async () => {
                const rows=newFile_FilePickerObject.getPickedFiles(document.getElementById('uploadAssignmentFormId'));
                return await Promise.all(rows.map(async x=> { const b=await x.file.arrayBuffer();
                    const h=await crypto.subtle.digest('SHA-256',b); return {filename:x.file.name,size_bytes:b.byteLength,
                    sha256:Array.from(new Uint8Array(h)).map(v=>v.toString(16).padStart(2,'0')).join('')}; })); }""")
            expected = [{k: f[k] for k in ('filename', 'size_bytes', 'sha256')} for f in plan['files']]
            require(actual == expected and not state['dialog'], '浏览器所选材料与准备记录不一致，或出现未适配提示。')
            advance('materials_verified')
            state['armed'] = True
            with page.expect_response(lambda r: r.url == post_url and r.request.method == 'POST', timeout=30000) as pending:
                page.locator('input[name="bottom_提交"]').click(timeout=10000)
            response = pending.value
            network.limiter.response('bb', response.status, response.headers)
            require(not state['error'], state['error'] or '')
            return {'http_status': response.status, 'post_requested': state['posted']}
        finally:
            # Read-only verification uses the existing cookies, even after an
            # unexpected response/navigation. Never reconstruct or replay POST.
            self.context.add_cookies(context.cookies())
            context.close()
