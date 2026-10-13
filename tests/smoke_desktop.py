"""Offline source/frozen UI acceptance with headless Chrome and a synthetic model API.

Never logs into a school, loads an existing profile, or sends real personal data.
"""
import argparse
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import sync_playwright, expect
from school_mcp.desktop.storage import Data, read_private
from school_mcp.bb.identity import save_session_identity
from school_mcp.script_jobs import JobStore, now


class Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        assert self.path == '/v1/models'
        assert self.headers['Authorization'] == 'Bearer synthetic-key'
        raw = json.dumps({'data':[{'id':'synthetic-model'},{'id':'synthetic-other'}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers(); self.wfile.write(raw)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert self.path == '/v1/chat/completions'
        assert self.headers['Authorization'] == 'Bearer synthetic-key'
        def call(name, args):
            return {'role': 'assistant', 'content': None, 'tool_calls': [{'type': 'function', 'id': 'call_' + name,
                    'function': {'name': name, 'arguments': json.dumps(args)}}]}
        if any(t['function']['name']=='connection_check' for t in body.get('tools', [])):
            results=[json.loads(m['content']) for m in body['messages'] if m['role']=='tool']
            message=({'role':'assistant','content':results[-1]['verification_code']} if results else call('connection_check', {'ok':True}))
        elif [m for m in body['messages'] if m['role'] == 'user'][-1]['content'] == 'synthetic-long-context':
            assert len(json.dumps(body, ensure_ascii=False).encode()) <= 192000
            assert len([m for m in body['messages'] if m['role'] == 'user']) == 9
            assert 'synthetic obsolete query' not in json.dumps(body)
            message = {'role': 'assistant', 'content': '长对话整理后可继续，原用户要求完整保留。'}
        else:
            results = [json.loads(m['content']) for m in body['messages'] if m['role'] == 'tool']
            if not results:
                message = call('school_agent_route', {'goal': '准备差旅报销材料'})
            elif len(results) == 1:
                message = call('school_agent_start', {'workflow': 'finance', 'goal': 'synthetic travel reimbursement'})
            elif len(results) == 2:
                message = call('school_agent_tools', {'task_id': results[1]['task_id'], 'tool_name': 'school_finance_preflight'})
            elif len(results) == 3:
                assert 'workflow_instructions' in results[2]
                message = call('school_agent_call', {'task_id': results[1]['task_id'], 'tool_name': 'school_finance_preflight',
                                                   'arguments': {'request': {'business_type': 'travel'}}})
            else:
                assert results[-1]['result']['safe_to_submit'] is False
                message = {'role': 'assistant', 'content': '材料预检完成，请补充出差日期和发票；尚未提交报销。'}
        raw = json.dumps({'choices': [{'message': message}]}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def wait_instance(directory, process):
    path = directory / 'instance.dpapi'
    end = time.monotonic() + 40
    while time.monotonic() < end:
        if process.poll() is not None:
            raise AssertionError('Application exited before startup')
        if path.exists():
            return read_private(path)
        time.sleep(.2)
    raise AssertionError('Application did not start')


def wait_operation(origin, headers):
    end = time.monotonic() + 20
    while time.monotonic() < end:
        status = httpx.get(origin + '/api/status', headers=headers, trust_env=False).json()
        if not status['busy']:
            return status
        time.sleep(.2)
    raise AssertionError('Synthetic operation did not finish')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe')
    args = parser.parse_args()
    logging.getLogger('httpx').setLevel(logging.WARNING)
    root = Path(__file__).resolve().parents[1]
    evidence = root / '.local' / 'desktop-evidence'
    evidence.mkdir(parents=True, exist_ok=True)
    kind = 'exe' if args.exe else 'source'
    command = [str(Path(args.exe).resolve())] if args.exe else [sys.executable, '-m', 'school_mcp.desktop.server']
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    errors = []
    with tempfile.TemporaryDirectory(prefix='school-desktop-smoke-') as tmp, ThreadingHTTPServer(('127.0.0.1', 0), Model) as model:
        directory = Path(tmp)
        threading.Thread(target=model.serve_forever, daemon=True).start()
        process = subprocess.Popen([*command, '--data-dir', tmp, '--no-open'], cwd=root, creationflags=flags)
        try:
            instance = wait_instance(directory, process)
            parsed = urlsplit(instance['url'])
            origin = f'{parsed.scheme}://{parsed.netloc}'
            headers = {'X-School-Token': parsed.fragment, 'Origin': origin}
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='chrome', headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1080}, device_scale_factor=1)
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(instance['url'])
                expect(page.locator('#stepIdentity')).to_contain_text('登录并完成验证')
                expect(page.locator('#setupCount')).to_have_text('0 / 2')
                page.screenshot(path=str(evidence / f'{kind}-onboarding.png'), full_page=True)
                # A synthetic verified identity is injected into only this isolated fixture.
                os.environ['SCHOOL_MCP_LOCAL_DIR'] = str(directory / 'profile')
                save_session_identity('synthetic-account')
                expect(page.locator('#stepIdentity')).to_contain_text('synthetic-account', timeout=12000)
                page.get_by_role('button', name='模型与设置').click()
                page.locator('#apiBase').fill(f'http://127.0.0.1:{model.server_port}/v1')
                page.locator('#apiModel').fill('synthetic-model')
                page.locator('#apiKey').fill('synthetic-key')
                page.get_by_role('button', name='保存并测试', exact=True).click()
                expect(page.locator('#apiKey')).to_have_value('')
                expect(page.locator('#modelStatus')).to_contain_text('测试已通过', timeout=15000)
                page.locator('#fetchModels').click()
                expect(page.locator('#modelOptions option')).to_have_count(2, timeout=15000)
                expect(page.locator('#startChat')).to_be_visible()
                page.screenshot(path=str(evidence / f'{kind}-settings.png'), full_page=True)
                page.get_by_role('button', name='账号与连接', exact=True).click()
                expect(page.locator('.service')).to_have_count(14)
                expect(page.locator('#serviceGrid').get_by_role('button', name='登录 / 验证', exact=True)).to_have_count(11)
                page.screenshot(path=str(evidence / f'{kind}-accounts.png'), full_page=True)
                page.get_by_role('button', name='模型与设置', exact=True).click()
                page.locator('#switchAccount').click()
                expect(page.locator('#confirmDialog')).to_be_visible()
                page.locator('#confirmDialog').get_by_role('button', name='取消', exact=True).click()
                assert httpx.get(origin + '/api/status', headers=headers, trust_env=False).json()['identity']['saved']
                page.get_by_role('button', name='对话', exact=False).first.click()
                expect(page.locator('#onboarding')).to_be_hidden()  # mail is optional
                page.locator('#fileInput').set_input_files({'name': 'synthetic-note.txt', 'mimeType': 'text/plain', 'buffer': b'synthetic material'})
                expect(page.locator('.file-chip')).to_have_count(1)
                page.get_by_role('button', name='移除材料 synthetic-note.txt').click()
                expect(page.locator('.file-chip')).to_have_count(0)
                page.locator('#messageInput').fill('请帮我准备差旅报销材料')
                page.locator('#send').click()
                expect(page.locator('#messages .assistant')).to_contain_text('材料预检完成', timeout=25000)
                page.screenshot(path=str(evidence / f'{kind}-chat.png'), full_page=True)
                page.get_by_role('button', name='任务记录').click()
                expect(page.locator('#taskList')).to_contain_text('待补材料', timeout=12000)
                expect(page.locator('.task-row[data-state="needs_input"]')).to_have_count(1)
                page.screenshot(path=str(evidence / f'{kind}-tasks.png'), full_page=True)
                # Opening twice must leave the current turn/database untouched.
                second = subprocess.run([*command, '--data-dir', tmp, '--no-open'], cwd=root, creationflags=flags, timeout=30)
                assert second.returncode == 0
                assert read_private(directory / 'instance.dpapi') == instance
                chat_id = httpx.get(origin + '/api/chats', headers=headers, trust_env=False).json()['chats'][0]['id']
                httpx.post(origin + '/api/shutdown', headers=headers, json={}, trust_env=False).raise_for_status()
                process.wait(15)
                process = subprocess.Popen([*command, '--data-dir', tmp, '--no-open'], cwd=root, creationflags=flags)
                instance = wait_instance(directory, process)
                page.goto(instance['url'])
                expect(page.locator('#modelBadge')).to_have_text('synthetic-model', timeout=12000)
                page.locator('#chatList button').first.click()
                expect(page.locator('#messages .assistant')).to_contain_text('材料预检完成')
                assert httpx.get(urlsplit(instance['url'])._replace(path='/api/chats', fragment='').geturl(),
                       headers={'X-School-Token': urlsplit(instance['url']).fragment}, trust_env=False).json()['chats'][0]['id'] == chat_id
                from school_mcp.desktop.artifacts import register
                import hashlib
                local_data = Data(tmp)
                output = local_data.profile / 'attachments' / 'synthetic-output.txt'
                output.parent.mkdir(exist_ok=True)
                output.write_bytes(b'synthetic downloaded output')
                saved = local_data.chat(chat_id)
                register(local_data, saved, 'school_mail_download_attachment', {'dispatched': True,
                    'tool': 'school_mail_download_attachment', 'result': {'path': str(output),
                    'filename': output.name, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}})
                local_data.save_chat(saved)
                expect(page.locator('#artifactFiles button')).to_have_count(1, timeout=12000)
                with page.expect_download() as event:
                    page.locator('#artifactFiles button').click()
                download = event.value
                assert download.suggested_filename == 'synthetic-output.txt'
                assert Path(download.path()).read_bytes() == output.read_bytes()
                # Small-screen layout must stay within the viewport.
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#historyToggle').click()
                expect(page.locator('#chatList')).to_be_visible()
                page.locator('#chatList button').first.click()
                expect(page.locator('#chatList')).not_to_be_visible()
                for view in ('accounts', 'tasks', 'settings', 'chat'):
                    page.locator('nav button[data-view="' + view + '"]').click()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), view
                    page.screenshot(path=str(evidence / f'{kind}-mobile-{view}.png'), full_page=True)
                assert not errors, errors
                # Exercise packaged context projection through its real HTTP/model path.
                current = urlsplit(instance['url'])
                origin = f'{current.scheme}://{current.netloc}'
                headers = {'X-School-Token': current.fragment, 'Origin': origin}
                data = Data(directory)
                long_chat = data.new_chat()
                for i in range(8):
                    long_chat['messages'] += [{'role': 'user', 'content': f'synthetic requirement {i}'},
                        {'role': 'assistant', 'content': 'synthetic obsolete query' * 20000}]
                data.save_chat(long_chat)
                httpx.post(origin + '/api/chat/send', headers=headers, json={
                    'chat_id': long_chat['id'], 'text': 'synthetic-long-context'}, trust_env=False).raise_for_status()
                assert wait_operation(origin, headers)['operation']['state'] == 'completed'
                saved = data.chat(long_chat['id'])
                assert saved['context_projection']['compacted']
                assert saved['messages'][:16] == long_chat['messages']
                assert '长对话整理后可继续' in saved['messages'][-1]['content']
                # Missing local session must stop before school traffic or automatic login.
                httpx.post(origin + '/api/connect', headers=headers, json={'service': 'bb'}, trust_env=False).raise_for_status()
                status = wait_operation(origin, headers)
                assert status['operation']['result']['error_code'] == 'authentication_required'
                assert status['operation']['state'] == 'attention'
                assert not status['operation']['result']['recovery']['automatic_retry']
                # Synthetic confirmation view: inspect an approval request without
                # dispatching it, then cancel through the real local backend.
                page.set_viewport_size({'width': 1440, 'height': 1000})
                pending = data.new_chat()
                pending.update(title='示例待确认任务', state='waiting_for_approval')
                arguments = {'task_id': 'a'*32, 'tool_name': 'school_bb_submit_assignment',
                             'arguments': {'preparation_id': 'b'*32, 'expected_sha256': 'c'*64}}
                pending['messages'] = [{'role': 'user', 'content': '示例：准备材料，先给我确认。'},
                    {'role': 'assistant', 'content': '示例材料已准备好，等待确认。', 'tool_calls': [
                        {'id': 'synthetic-confirm', 'type': 'function', 'function': {
                            'name': 'school_agent_call', 'arguments': json.dumps(arguments)}}]}]
                pending['pending'] = {'id': 'synthetic-approval', 'name': 'school_agent_call',
                    'arguments': arguments, 'call_id': 'synthetic-confirm',
                    'preview': {'目标': '示例作业', '材料': ['示例材料.pdf'],
                                '说明': '<img src="/should-not-load" onerror="window.unsafePreview=true">'}}
                data.save_chat(pending)
                page.goto(instance['url'])
                page.locator('#chatList button', has_text='示例待确认任务').click()
                expect(page.locator('#approval')).to_be_visible()
                expect(page.locator('#approvalPreview img')).to_have_count(0)
                assert page.evaluate('window.unsafePreview !== true')
                expect(page.locator('#executePermit')).not_to_be_checked()
                page.screenshot(path=str(evidence / f'{kind}-approval.png'), full_page=True)
                approval_requests = []
                def intercept_approval(route):
                    approval_requests.append(route.request.post_data_json)
                    route.fulfill(json={'started': True})
                page.route('**/api/chat/approve', intercept_approval)
                with page.expect_response('**/api/chat/approve'):
                    page.locator('#approve').click()
                assert approval_requests == [{'chat_id': pending['id'], 'approval_id': 'synthetic-approval', 'approve': True}]
                page.unroute('**/api/chat/approve', intercept_approval)
                page.locator('#reject').click()
                expect(page.locator('#approval')).not_to_be_visible()
                assert data.chat(pending['id'])['pending'] is None
                assert not data.chat(pending['id'])['turn_authorized']
                page.get_by_role('button', name='模型与设置', exact=True).click()
                page.locator('#apiModel').fill('unsaved-synthetic-model')
                with page.expect_response('**/api/status'):
                    pass  # Wait for the regular poll; it must not overwrite form input.
                expect(page.locator('#apiModel')).to_have_value('unsaved-synthetic-model')
                assert not errors, errors
                browser.close()
            if args.exe:
                store = JobStore()
                job = store.create('jw', 'jw_watch', {}, now().isoformat(), (now()+timedelta(minutes=1)).isoformat(), 60, None)
                store.update(job, end_at=(now()-timedelta(seconds=1)).isoformat())
                assert store.claim(job)
                worker = subprocess.run([*command, '-X', 'utf8', '-m', 'school_mcp', 'script-worker', '--job-id', job,
                                         '--private-dir', str(directory / 'profile')], cwd=root, creationflags=flags, timeout=30)
                assert worker.returncode == 0
                assert store.get(job)['state'] == 'expired'
                with tempfile.TemporaryDirectory(prefix='school-packaged-check-') as check_dir:
                    result = subprocess.run([*command, '--data-dir', check_dir, '--self-check'], cwd=root, creationflags=flags, timeout=45)
                    assert result.returncode == 0
                    assert json.loads((Path(check_dir)/'runtime-check.json').read_text(encoding='utf-8'))['chrome_headless']
            print(json.dumps({'passed': True, 'target': kind, 'school_network_calls': 0,
                'checks': ['onboarding', 'model-function-call', 'chat-real-finance-preflight', 'tasks', 'second-launch', 'restart', 'mobile-four-views-history', 'no-js-errors', 'long-context-original-history', 'typed-auth-failure', 'fourteen-services', 'cancel-account-reset', 'upload-remove', 'approval-request-intercepted', 'real-cancel-approval', 'safe-preview-text', 'form-retained-on-poll'],
                'synthetic_identity': True, 'real_manual_login_tested': False}))
        finally:
            if process.poll() is None:
                path = directory / 'instance.dpapi'
                if path.exists():
                    current = urlsplit(read_private(path)['url'])
                    origin = f'{current.scheme}://{current.netloc}'
                    try:
                        httpx.post(origin+'/api/shutdown', headers={'X-School-Token': current.fragment, 'Origin': origin}, json={}, trust_env=False)
                        process.wait(10)
                    except Exception:
                        process.terminate()
                        process.wait(10)
            model.shutdown()


if __name__ == '__main__':
    main()
