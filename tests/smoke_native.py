"""Real hidden Tauri/WebView2 acceptance via Playwright CDP; synthetic data only."""
import argparse
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

import httpx
from playwright.sync_api import sync_playwright, expect
from school_mcp.desktop.storage import Data, read_private, write_private
from school_mcp.desktop.artifacts import register
from school_mcp.bb.identity import save_session_identity
from smoke_desktop import Model
from test_campus_pages import SCHEDULE, SEMESTERS


def main():
    p=argparse.ArgumentParser();p.add_argument('--exe',required=True);args=p.parse_args()
    logging.getLogger('httpx').setLevel(logging.WARNING)
    evidence=Path(__file__).resolve().parents[1]/'.local/native-evidence';evidence.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='school-native-smoke-') as tmp, ThreadingHTTPServer(('127.0.0.1',0),Model) as model:
        threading.Thread(target=model.serve_forever,daemon=True).start()
        with socket.socket() as sock: sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        env=dict(os.environ,WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f'--remote-debugging-port={port}')
        cmd=[str(Path(args.exe).resolve()),'--data-dir',tmp,'--hidden']
        process=subprocess.Popen(cmd,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
        browser=None;errors=[]
        try:
            end=time.monotonic()+60
            with httpx.Client(trust_env=False,timeout=1) as http:
                while time.monotonic()<end:
                    assert process.poll() is None,'Native process exited'
                    try:
                        if http.get(f'http://127.0.0.1:{port}/json/version').is_success:break
                    except httpx.HTTPError:pass
                    time.sleep(.2)
                else:raise AssertionError('WebView2 CDP did not start')
            with sync_playwright() as pw:
                browser=pw.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
                page=browser.contexts[0].pages[0]
                page.on('pageerror',lambda err:errors.append(str(err)))
                expect(page.locator('#setupCount')).to_have_text('0 / 2',timeout=60000)
                assert page.url.startswith('http://tauri.localhost')
                assert page.evaluate("sessionStorage.getItem('school-token')") is None
                assert not page.evaluate('location.hash')
                ready=page.evaluate("async()=> (await schoolFetch('/api/status')).json()")
                assert Path(ready['data_location'])==Path(tmp)
                original=read_private(Path(tmp)/'instance.dpapi')
                assert original is not None
                assert original['core']['implementation']=='rust'
                assert original['core']['version']=='0.6.0'
                assert 'finance_preflight' in original['core']['capabilities']
                assert {'runner_step', 'agent_claim', 'catalog_route', 'recovery_failure'} <= set(original['core']['capabilities'])
                assert {'network_policy', 'public_read', 'sessions_read', 'sessions_merge', 'browser_start'} <= set(original['core']['capabilities'])
                assert {'authenticated_read', 'jw', 'bb_adapter', 'young_policy', 'young_job', 'young_parse'} <= set(original['core']['capabilities'])
                # Shared WebView2 user-data requires identical browser arguments.
                second=subprocess.Popen(cmd,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    time.sleep(2)
                    assert second.poll() is None
                    after=read_private(Path(tmp)/'instance.dpapi')
                    assert after==original,{'before_pid':original.get('pid'),'after_pid':after.get('pid'),
                        'primary_exit':process.poll(),'second_exit':second.poll()}
                finally:second.terminate();second.wait(10)
                assert page.evaluate("async()=> (await schoolFetch('/api/status')).ok")
                page.locator('nav [data-view="sites"]').click()
                expect(page.locator('#siteGrid a')).to_have_count(17)
                expect(page.locator('#notice')).to_be_hidden()
                page.screenshot(path=str(evidence/'native-sites.png'))
                # Transport must reject remote requests, unknown routes and oversized JSON.
                for method,path in [('GET','https://example.test/'),('GET','/api/status?url=x'),('POST','/api/unknown')]:
                    rejected=page.evaluate("""async ({method,path})=>{try{await schoolFetch(path,{method});return false;}catch{return true;}}""",{'method':method,'path':path})
                    assert rejected,path
                assert page.evaluate("""async()=>{try{await __TAURI__.core.invoke('plugin:shell|execute',{program:'cmd'});return false;}catch{return true;}}""")
                status=page.evaluate("async()=> (await schoolFetch('/api/campus/profile')).status")
                assert status==400,'Native transport must retain backend authorization errors'
                data=Data(tmp);save_session_identity('synthetic-account')
                write_private(data.profile/'timetable-view.dpapi',{'account':'synthetic-account','updated_at':'2026-01-01T10:00:00+08:00','data':SCHEDULE,'semesters':SEMESTERS['semesters']})
                expect(page.locator('#sideIdentity')).to_have_text('synthetic-account',timeout=12000)
                page.locator('nav [data-view="profile"]').click()
                expect(page.locator('#saveProfile')).to_be_enabled()
                name=page.locator('#profileFields input').first;name.fill('合成同学');page.locator('#saveProfile').click()
                expect(page.locator('#profileStatus')).to_have_text(re.compile('^已保存到本机'))
                assert '合成同学' in str(read_private(data.profile/'personal-profile.dpapi'))
                page.locator('nav [data-view="settings"]').click()
                page.locator('#apiBase').fill(f'http://127.0.0.1:{model.server_port}/v1')
                page.locator('#apiModel').fill('synthetic-model');page.locator('#apiKey').fill('synthetic-key')
                page.locator('#saveModel').click()
                expect(page.locator('#modelStatus')).to_contain_text('测试已通过',timeout=25000)
                expect(page.locator('#apiKey')).to_have_value('')
                page.locator('nav [data-view="chat"]').click()
                page.locator('#fileInput').set_input_files({'name':'synthetic-note.txt','mimeType':'text/plain','buffer':b'synthetic material'})
                expect(page.locator('.file-chip')).to_have_count(1)
                page.get_by_role('button',name='移除材料 synthetic-note.txt').click()
                page.locator('#messageInput').fill('请帮我准备差旅报销材料');page.locator('#send').click()
                expect(page.locator('#messages .assistant')).to_contain_text('材料预检完成',timeout=35000)
                chat=data.chat(data.chats()[0]['id'])
                output=data.profile/'attachments/synthetic-output.txt';output.parent.mkdir(exist_ok=True);output.write_bytes(b'synthetic downloaded output')
                register(data,chat,'school_mail_download_attachment',{'dispatched':True,'tool':'school_mail_download_attachment',
                    'result':{'path':str(output),'filename':output.name,'sha256':hashlib.sha256(output.read_bytes()).hexdigest()}})
                data.save_chat(chat)
                artifact=chat['artifacts'][0]
                actual=page.evaluate("""async p=>Array.from(new Uint8Array(await (await schoolFetch(p)).arrayBuffer()))""",
                    f"/api/artifact?chat={chat['id']}&id={artifact['id']}")
                assert bytes(actual)==output.read_bytes(),'Binary IPC response corrupted'
                # Exercise the actual download button without a foreground save dialog.
                download_dir=Path(tmp)/'download-check';download_dir.mkdir()
                cdp=page.context.new_cdp_session(page)
                cdp.send('Browser.setDownloadBehavior',{'behavior':'allow','downloadPath':str(download_dir)})
                expect(page.locator('#artifactFiles button')).to_have_count(1,timeout=12000)
                page.locator('#artifactFiles button').click()
                end=time.monotonic()+15
                while time.monotonic()<end:
                    target=download_dir/output.name
                    if target.exists() and target.read_bytes()==output.read_bytes():break
                    time.sleep(.2)
                else:raise AssertionError('Native download button did not produce the original file')
                page.locator('nav [data-view="timetable"]').click()
                expect(page.locator('#timetableBoard')).to_contain_text('合成课程')
                page.screenshot(path=str(evidence/'native-timetable.png'))
                page.locator('nav [data-view="settings"]').click()
                page.locator('#refreshDiagnostics').click()
                expect(page.locator('#diagnosticsJson')).not_to_be_empty()
                page.locator('nav [data-view="accounts"]').click()
                page.locator('#lockApp').click()
                page.locator('#confirmAction').click()
                expect(page.locator('#sideIdentity')).to_have_text('尚未登录',timeout=12000)
                assert (page.evaluate("async()=> (await schoolFetch('/api/campus/profile')).status"))==400
                assert not errors,errors
                # Exit is a real IPC shutdown of only the owned test engine.
                awaitable="async()=>{await schoolFetch('/api/shutdown',{method:'POST',body:'{}'});return true;}"
                assert page.evaluate(awaitable)
                process.wait(15)
                end=time.monotonic()+10
                while (Path(tmp)/'instance.dpapi').exists() and time.monotonic()<end:time.sleep(.2)
                assert not (Path(tmp)/'instance.dpapi').exists()
                browser=None
            print(json.dumps({'passed':True,'runtime':'Tauri + Rust + real hidden WebView2','school_network_calls':0,
                'checks':['IPC auth and route bounds','no token in WebView','17 websites','profile encrypted save',
                    'synthetic model tool call','finance preflight','binary upload/download','cached timetable','lock','owned shutdown','second native launch reuses engine'],
                'real_login_tested':False},ensure_ascii=False))
        finally:
            if process.poll() is None:process.terminate();process.wait(10)
            # EOF on the host pipe lets the isolated business engine stop cleanly.
            end=time.monotonic()+15
            while (Path(tmp)/'instance.dpapi').exists() and time.monotonic()<end:time.sleep(.2)
            model.shutdown()


if __name__=='__main__':main()
