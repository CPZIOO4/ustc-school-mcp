"""Daily-use acceptance against a real packaged Tauri/WebView2 application.

Uses a fresh encrypted profile and a loopback synthetic model. No school login,
school mutation, real model billing or existing user data is involved.
"""
from __future__ import annotations

import argparse
from collections import Counter
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time

import httpx
from playwright.sync_api import sync_playwright, expect

from school_mcp.bb.identity import save_session_identity
from school_mcp.desktop.storage import Data, read_private, write_private
from smoke_desktop import Model
from test_campus_pages import SCHEDULE, SEMESTERS


class DailyModel(Model):
    calls = Counter()
    slow_started = threading.Event()
    release_slow = threading.Event()

    def do_POST(self):
        # The parent fixture checks an actual two-step tool capability handshake.
        size = int(self.headers['Content-Length'])
        raw = self.rfile.read(size)
        body = json.loads(raw)
        if any(t['function']['name'] == 'connection_check' for t in body.get('tools', [])):
            from io import BytesIO
            self.rfile = BytesIO(raw)
            return super().do_POST()
        users = [m['content'] for m in body['messages'] if m['role'] == 'user']
        query = users[-1]
        type(self).calls[query] += 1
        if query == '模拟一次模型服务故障':
            payload = b'{"error":"synthetic upstream unavailable"}'
            self.send_response(503)
        else:
            if query == '模拟慢响应后停止':
                type(self).slow_started.set()
                type(self).release_slow.wait(15)
                text = '不应显示的晚到模型回答'
            elif query == '生成长回答用于阅读':
                text = '\n\n'.join(f'第 {n} 条：这是一段合成校园事项说明，用于检验阅读时不会被后台刷新拖回底部。' for n in range(1, 61))
            else:
                text = '已处理合成请求：' + query
            payload = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': text}}]}, ensure_ascii=False).encode()
            self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def available_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_cdp(port, process):
    deadline = time.monotonic() + 65
    with httpx.Client(trust_env=False, timeout=.8) as client:
        while time.monotonic() < deadline:
            assert process.poll() is None, 'Native host exited before WebView2 started'
            try:
                if client.get(f'http://127.0.0.1:{port}/json/version').is_success:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.15)
    raise AssertionError('WebView2 did not become ready')


def api(page, path):
    return page.evaluate('async p => (await schoolFetch(p)).json()', path)


def idle(page, timeout=25000):
    page.wait_for_function("async()=>!(await (await schoolFetch('/api/status')).json()).busy", timeout=timeout)
    # Ask the real UI to reconcile the final operation before assertions.
    page.evaluate('refresh()')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', required=True)
    parser.add_argument('--evidence', default='.local/ux-20261018/native-daily')
    args = parser.parse_args()
    evidence = Path(args.evidence).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    report = {'passed': False, 'runtime': 'packaged Tauri + Rust IPC + real WebView2',
              'school_calls': 0, 'paid_model_calls': 0, 'checks': [], 'timings_seconds': {}}
    errors = []
    process = None
    DailyModel.calls.clear()
    DailyModel.slow_started.clear()
    DailyModel.release_slow.clear()
    with tempfile.TemporaryDirectory(prefix='school-daily-') as tmp, ThreadingHTTPServer(('127.0.0.1', 0), DailyModel) as model:
        threading.Thread(target=model.serve_forever, daemon=True).start()
        port = available_port()
        env = dict(os.environ, WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f'--remote-debugging-port={port}')
        command = [str(Path(args.exe).resolve()), '--data-dir', tmp, '--hidden']
        try:
            started = time.monotonic()
            process = subprocess.Popen(command, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            wait_cdp(port, process)
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
                page = browser.contexts[0].pages[0]
                page.on('pageerror', lambda err: errors.append(str(err)))
                expect(page.locator('#setupCount')).to_have_text('0 / 2', timeout=60000)
                report['timings_seconds']['fresh_start_to_ready'] = round(time.monotonic()-started, 3)
                expect(page.locator('#send')).to_be_disabled()
                page.screenshot(path=str(evidence/'01-first-use.png'))
                page.locator('nav [data-view="sites"]').click()
                expect(page.locator('#siteGrid a')).to_have_count(17)
                page.locator('#siteSearch').fill('不存在的合成网站')
                expect(page.locator('#siteGrid')).to_contain_text('没有')
                page.locator('#siteSearch').fill('')
                expect(page.locator('#siteGrid a')).to_have_count(17)
                report['checks'].append('First-use gates and website search work without a school login')

                data = Data(tmp)
                save_session_identity('synthetic-daily-user')
                write_private(data.profile/'timetable-view.dpapi', {'account': 'synthetic-daily-user', 'updated_at': '2026-01-01T10:00:00+08:00', 'data': SCHEDULE, 'semesters': SEMESTERS['semesters']})
                page.evaluate('refresh()')
                expect(page.locator('#sideIdentity')).to_have_text('synthetic-daily-user', timeout=12000)
                page.locator('nav [data-view="settings"]').click()
                page.locator('#apiBase').fill(f'http://127.0.0.1:{model.server_port}/v1')
                page.locator('#apiKey').fill('synthetic-key')
                page.locator('#apiModel').fill('synthetic-model')
                page.locator('#saveModel').click()
                expect(page.locator('#modelStatus')).to_contain_text('测试已通过', timeout=25000)
                expect(page.locator('#apiKey')).to_have_value('')
                report['checks'].append('A real two-step tool-call capability handshake enables conversation')

                page.locator('nav [data-view="chat"]').click()
                page.locator('#messageInput').fill('第一段合成对话')
                page.locator('#send').click()
                expect(page.locator('#messages .assistant')).to_contain_text('已处理合成请求', timeout=25000)
                idle(page)
                page.locator('#messageInput').fill('第一段对话未发送的草稿')
                page.locator('#fileInput').set_input_files({'name':'仅用于测试的长文件名_课程材料_不包含个人信息.txt', 'mimeType':'text/plain', 'buffer':b'synthetic material'})
                expect(page.locator('.file-chip')).to_have_count(1)
                page.locator('#newChat').click()
                expect(page.locator('#messageInput')).to_have_value('')
                expect(page.locator('.file-chip')).to_have_count(0)
                page.locator('#messageInput').fill('第二段合成对话')
                page.locator('#send').click()
                expect(page.locator('#messages .assistant')).to_contain_text('第二段合成对话', timeout=25000)
                idle(page)
                page.locator('#chatList button').filter(has_text='第一段合成对话').click()
                expect(page.locator('#messageInput')).to_have_value('第一段对话未发送的草稿')
                expect(page.locator('.file-chip')).to_have_count(1)
                page.locator('.file-chip button').click()
                expect(page.locator('.file-chip')).to_have_count(0)
                report['checks'].append('Draft text and attachments stay with their conversation when switching')

                page.locator('#messageInput').fill('生成长回答用于阅读')
                page.locator('#send').click()
                expect(page.locator('#messages')).to_contain_text('第 60 条', timeout=25000)
                idle(page)
                page.locator('#conversationScroll').evaluate("e=>e.scrollTo({top:120,behavior:'instant'})")
                before_scroll = page.locator('#conversationScroll').evaluate('e=>e.scrollTop')
                page.wait_for_timeout(9500)
                after_scroll = page.locator('#conversationScroll').evaluate('e=>e.scrollTop')
                assert abs(before_scroll-after_scroll) < 5, (before_scroll, after_scroll)
                page.screenshot(path=str(evidence/'02-reading-long-answer.png'))
                report['checks'].append('Background refresh preserves the reading position in long replies')

                page.locator('#newChat').click()
                page.locator('#messageInput').fill('模拟一次模型服务故障')
                page.locator('#send').click()
                idle(page)
                status = api(page, '/api/status')
                assert status['operation']['state'] == 'interrupted', status['operation']['state']
                assert status['operation']['result']['error_code'] == 'model_http_error', status['operation']['result']
                expect(page.locator('#progress')).to_be_visible()
                expect(page.locator('#progress')).to_contain_text('模型服务返回错误状态')
                page.wait_for_timeout(9500)
                assert DailyModel.calls['模拟一次模型服务故障'] == 1, 'Model failure was retried silently'
                page.screenshot(path=str(evidence/'03-model-failure.png'))
                page.locator('#messageInput').fill('模型恢复后手动继续')
                expect(page.locator('#send')).to_be_enabled()
                page.locator('#send').click()
                expect(page.locator('#messages')).to_contain_text('已处理合成请求：模型恢复后手动继续', timeout=25000)
                idle(page)
                report['checks'].append('Model 503 does not auto-retry; the user can continue after recovery')

                page.locator('#messageInput').fill('模拟慢响应后停止')
                page.locator('#send').click()
                deadline = time.monotonic()+10
                while not DailyModel.slow_started.is_set() and time.monotonic()<deadline:
                    page.wait_for_timeout(100)
                assert DailyModel.slow_started.is_set()
                expect(page.locator('#stop')).to_be_visible(timeout=5000)
                cached_start = time.monotonic()
                page.locator('nav [data-view="timetable"]').click()
                expect(page.locator('#timetableBoard')).to_contain_text('合成课程', timeout=5000)
                report['timings_seconds']['cached_timetable_during_model_wait'] = round(time.monotonic()-cached_start, 3)
                expect(page.locator('#operationBar')).to_be_visible()
                page.locator('#operationStop').click()
                idle(page, timeout=10000)
                stopped = api(page, '/api/status')
                assert stopped['operation']['state'] in ('interrupted', 'stopped'), stopped['operation']['state']
                DailyModel.release_slow.set()
                page.wait_for_timeout(1200)
                page.locator('nav [data-view="chat"]').click()
                expect(page.locator('#messages')).not_to_contain_text('不应显示的晚到模型回答')
                assert DailyModel.calls['模拟慢响应后停止'] == 1
                report['checks'].append('Cached timetable remains accessible during model wait; global stop from another view suppresses late replies')

                # Repeated everyday navigation must not create duplicate chats or lose saved content.
                count_before = len(data.chats())
                for _ in range(4):
                    for view in ('sites', 'timetable', 'profile', 'tasks', 'settings', 'chat'):
                        page.locator(f'nav [data-view="{view}"]').click()
                        expect(page.locator('#view-'+view)).to_be_visible()
                assert len(data.chats()) == count_before
                report['checks'].append('24 successive view changes retain data and do not create duplicate conversations')
                assert not errors, errors
                page.evaluate("async()=>schoolFetch('/api/shutdown',{method:'POST',body:'{}'})")
                process.wait(15)
                browser.close()
                deadline = time.monotonic()+15
                while (Path(tmp)/'instance.dpapi').exists() and time.monotonic()<deadline:
                    time.sleep(.2)
                # WebView2's old debugging listener can outlive the closed host
                # briefly. A fresh port prevents mistaking it for the new app.
                port = available_port()
                env['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = f'--remote-debugging-port={port}'
                process = subprocess.Popen(command, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
                wait_cdp(port, process)
                browser = pw.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
                page = browser.contexts[0].pages[0]
                expect(page.locator('#sideIdentity')).to_have_text('synthetic-daily-user', timeout=60000)
                page.locator('#chatList button').filter(has_text='第一段合成对话').click()
                expect(page.locator('#messages')).to_contain_text('第 60 条', timeout=12000)
                page.locator('nav [data-view="timetable"]').click()
                expect(page.locator('#timetableBoard')).to_contain_text('合成课程')
                assert api(page, '/api/status')['model']['tested'] is True
                assert len(data.chats()) == count_before
                assert read_private(data.settings_path) is not None
                report['checks'].append('Application restart retains encrypted model configuration, sent chat history and cached timetable')
                page.screenshot(path=str(evidence/'04-restarted-timetable.png'))
                page.evaluate("async()=>schoolFetch('/api/shutdown',{method:'POST',body:'{}'})")
                process.wait(15)
                report['passed'] = True
        except Exception as exc:
            report['failure'] = str(exc)
            if 'page' in locals():
                try:
                    page.screenshot(path=str(evidence/'failure.png'))
                except Exception:
                    pass
            raise
        finally:
            DailyModel.release_slow.set()
            if process and process.poll() is None:
                process.terminate()
                process.wait(10)
            deadline = time.monotonic()+15
            while (Path(tmp)/'instance.dpapi').exists() and time.monotonic()<deadline:
                time.sleep(.2)
            model.shutdown()
            report['page_errors'] = errors
            report['synthetic_model_calls'] = dict(DailyModel.calls)
            (evidence/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
