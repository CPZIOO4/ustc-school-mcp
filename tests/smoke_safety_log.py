"""Actual headless Chrome + source/frozen process fault acceptance, no school access."""
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import sync_playwright, expect
from school_mcp.desktop.storage import Data, read_private, write_private
from school_mcp.bb.identity import save_session_identity

CANARY='SYNTHETIC-PRIVATE-CANARY-NEVER-EXPORT'


class FailedModel(BaseHTTPRequestHandler):
    calls=0
    def log_message(self,*args):pass
    def do_POST(self):
        type(self).calls+=1
        self.rfile.read(int(self.headers['Content-Length']))
        raw=CANARY.encode()
        self.send_response(429);self.send_header('Content-Length',str(len(raw)))
        self.end_headers();self.wfile.write(raw)


def instance(data,process):
    for _ in range(180):
        assert process.poll() is None,'isolated app exited'
        path=data.root/'instance.dpapi'
        if path.exists():return read_private(path)
        time.sleep(.2)
    raise AssertionError('isolated app did not start')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--exe');args=parser.parse_args()
    logging.getLogger('httpx').setLevel(logging.WARNING)
    project=Path(__file__).resolve().parents[1]
    kind='exe' if args.exe else 'source'
    evidence=project/'.local'/('safety-ui-'+kind);evidence.mkdir(parents=True,exist_ok=True)
    command=[str(Path(args.exe).resolve())] if args.exe else [sys.executable,'-m','school_mcp.desktop.server']
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    result={'kind':kind,'school_network_used':False,'synthetic_model':True}
    with tempfile.TemporaryDirectory(prefix='school-safety-') as tmp, ThreadingHTTPServer(('127.0.0.1',0),FailedModel) as model:
        threading.Thread(target=model.serve_forever,daemon=True).start()
        data=Data(tmp)
        def launch():return subprocess.Popen([*command,'--data-dir',tmp,'--no-open'],cwd=project,creationflags=flags,
                                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        process=launch()
        try:
            info=instance(data,process)
            parsed=urlsplit(info['url']);origin=f'{parsed.scheme}://{parsed.netloc}'
            headers={'X-School-Token':parsed.fragment,'Origin':origin}
            errors=[]
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='chrome',headless=True)
                context=browser.new_context(permissions=['clipboard-read','clipboard-write'],viewport={'width':1360,'height':1000})
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(info['url']);page.get_by_role('button',name='模型与设置',exact=True).click()
                page.locator('#apiBase').fill(f'http://127.0.0.1:{model.server_port}/{CANARY}/v1')
                page.locator('#apiKey').fill(CANARY);page.locator('#apiModel').fill('synthetic')
                page.locator('#saveModel').click()
                expect(page.locator('#modelStatus')).to_contain_text('限流',timeout=20000)
                page.locator('#refreshDiagnostics').click()
                expect(page.locator('#diagnosticsEvents')).to_contain_text('model_rate_limited')
                preview=json.loads(page.locator('#diagnosticsJson').text_content())
                assert preview['events'] and all(len(e['build'])==64 for e in preview['events'])
                assert CANARY not in json.dumps(preview)
                assert tmp not in json.dumps(preview)
                page.locator('#copyDiagnostics').click()
                copied=page.evaluate('navigator.clipboard.readText()')
                assert preview['events'][0]['error_id'] in copied and CANARY not in copied
                with page.expect_download() as download:
                    page.locator('#exportDiagnostics').click()
                output=evidence/'diagnostics.json';download.value.save_as(output)
                assert json.loads(output.read_text(encoding='utf-8'))==preview
                result['model_failure_export_copy']=True
                assert FailedModel.calls==1,'model request unexpectedly retried'
                assert httpx.get(origin+'/api/diagnostics/export',trust_env=False).status_code==403
                page.locator('#diagnosticsTitle').scroll_into_view_if_needed()
                page.screenshot(path=str(evidence/'desktop.png'))
                page.set_viewport_size({'width':390,'height':844})
                page.locator('#diagnosticsTitle').scroll_into_view_if_needed()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(evidence/'mobile.png'))
                # Seed an interrupted local turn, then kill only this test-owned process.
                save_session_identity('synthetic-account')
                chat=data.new_chat();chat['state']='running';data.save_chat(chat)
                write_private(data.root/'operation.dpapi',{'state':'running','kind':'chat'})
                process.kill();process.wait(timeout=10)
                (data.root/'instance.dpapi').unlink(missing_ok=True)
                process=launch();info=instance(data,process)
                parsed=urlsplit(info['url']);origin=f'{parsed.scheme}://{parsed.netloc}'
                headers={'X-School-Token':parsed.fragment,'Origin':origin}
                page.goto(info['url']);page.get_by_role('button',name='模型与设置',exact=True).click()
                page.locator('#refreshDiagnostics').click()
                expect(page.locator('#diagnosticsEvents')).to_contain_text('interrupted')
                assert data.chat(chat['id'])['state']=='interrupted'
                assert FailedModel.calls==1
                result['hard_restart_no_replay']=True
                # Explicit UI clear must retain login/model/chat and original operation.
                page.locator('#clearDiagnostics').click();expect(page.locator('#confirmDialog')).to_be_visible()
                page.locator('#confirmAction').click()
                expect(page.locator('#diagnosticsStatus')).to_contain_text('没有诊断错误')
                assert data.chat(chat['id'])['state']=='interrupted'
                assert read_private(data.settings_path)['api_key']==CANARY
                result['clear_preserves_business_data']=True
                assert not errors,errors
                result['no_js_errors']=True
                response=httpx.post(origin+'/api/shutdown',headers=headers,json={},trust_env=False)
                response.raise_for_status();process.wait(timeout=15)
                browser.close()
            # Offline export works even when the school identity cannot decrypt.
            (data.profile/'ustc-identity.credentials.dpapi').write_bytes(b'corrupt synthetic identity')
            offline=evidence/'offline.json';offline.unlink(missing_ok=True)
            completed=subprocess.run([*command,'--data-dir',tmp,'--export-diagnostics',str(offline)],cwd=project,
                                     creationflags=flags,timeout=30,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            assert completed.returncode==0 and json.loads(offline.read_text(encoding='utf-8'))['available']
            result['offline_export_without_credentials']=True
            result['passed']=True
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=10)
            model.shutdown()
    (evidence/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':main()
