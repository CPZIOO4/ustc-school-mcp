"""Headless Chrome against real local API and encrypted synthetic profile. No school writes."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import httpx
from playwright.sync_api import sync_playwright,expect
from school_mcp.desktop.storage import Data,read_private
from school_mcp.bb.identity import save_session_identity
from school_mcp.digest import engine,public
from school_mcp.digest.store import Store,defaults
from school_mcp.digest.worker import running
from school_mcp.digest.calendar import CST
from datetime import datetime,timedelta

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--exe');args=parser.parse_args()
    project=Path(__file__).resolve().parents[1];out=project/'.local'/('digest-ui-exe' if args.exe else 'digest-ui-source');out.mkdir(exist_ok=True)
    command=[str(Path(args.exe).resolve())] if args.exe else [sys.executable,'-m','school_mcp.desktop.server']
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    with tempfile.TemporaryDirectory(prefix='school-digest-') as tmp:
        data=Data(tmp);save_session_identity('synthetic-digest-account')
        engine.configure(defaults()|{'sources':['jobs'],'enabled':True,'urgent_hours':0},True)
        now=datetime(2026,10,12,8,tzinfo=CST)
        row=public.item('jobs','synthetic','合成讲座：<script>不能执行</script>','https://www.job.ustc.edu.cn/',start='2026-10-13T10:00:00+08:00',end='2026-10-13T11:00:00+08:00',summary='合成验收资料',location='合成会议室')
        engine.refresh(notify=True,now=now,collector=lambda *_:dict(items=[],complete=True))
        engine.refresh(notify=True,now=now+timedelta(days=1),collector=lambda *_:dict(items=[row],complete=True))
        engine.configure(defaults()|{'sources':['jobs'],'enabled':False},False)
        state=Store().read();state['settings']['enabled']=True;state['last_cycle']=(datetime.now(CST)+timedelta(days=1)).isoformat();Store().save(state)
        process=subprocess.Popen([*command,'--data-dir',tmp,'--no-open'],cwd=project,creationflags=flags,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(150):
                if (data.root/'instance.dpapi').exists():break
                assert process.poll() is None;time.sleep(.2)
            instance=read_private(data.root/'instance.dpapi');u=urlsplit(instance['url']);origin=f'{u.scheme}://{u.netloc}'
            headers={'X-School-Token':u.fragment,'Origin':origin}
            for _ in range(100):
                if running():break
                time.sleep(.2)
            assert running(),'application did not restore worker'
            engine.configure(Store().read()['settings']|{'enabled':False})
            for _ in range(60):
                if not running():break
                time.sleep(.2)
            assert not running(),'worker did not stop after pause'
            errors=[]
            assert httpx.get(origin+'/api/digest',trust_env=False).status_code==403
            assert httpx.post(origin+'/api/digest/configure',json={},headers={'X-School-Token':u.fragment,'Origin':'https://evil.test'},trust_env=False).status_code==403
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='chrome',headless=True)
                page=browser.new_page(viewport={'width':1360,'height':1000});page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(instance['url']);page.locator('[data-view=digest]').click()
                expect(page.locator('#digestItems')).to_contain_text('合成讲座')
                expect(page.locator('#digestMessages')).to_contain_text('未读')
                page.get_by_role('button',name='标记已读',exact=True).click();expect(page.locator('#digestMessages')).to_contain_text('已读')
                page.locator('#digestPresetPlan').click()
                page.get_by_role('button',name='应用到表单（尚不保存）',exact=True).wait_for(timeout=20000)
                assert Store().read()['settings']['sources']==['jobs']
                page.get_by_role('button',name='应用到表单（尚不保存）',exact=True).click()
                assert page.locator('#digestSources input:checked').count()==7
                page.locator('#digestKeywords').fill('合成')
                page.locator('#digestItems input[type=checkbox]').first.check()
                with Store().cycle():
                    state=Store().read();message=dict(state['messages'][0],id='synthetic-background',read=False,text='后台更新合成简报');state['messages'].append(message);Store().save(state)
                expect(page.locator('#digestMessages')).to_contain_text('后台更新合成简报',timeout=22000)
                assert page.locator('#digestKeywords').input_value()=='合成'
                assert page.locator('#digestItems input[type=checkbox]').first.is_checked()
                page.get_by_role('button',name='保存范围并暂停推送',exact=True).click()
                expect(page.locator('#toast')).to_contain_text('已保存')
                page.locator('#digestPreviewReport').click()
                expect(page.locator('#digestMessages')).to_contain_text('【试阅简报】',timeout=20000)
                assert not Store().read()['settings']['email_enabled']
                page.locator('#digestItems input[type=checkbox]').first.check();page.locator('#digestExport').click()
                page.get_by_role('button',name='打开日历下载',exact=True).wait_for(timeout=25000)
                page.get_by_role('button',name='打开日历下载',exact=True).click()
                # Artifact download uses the normal authenticated chat download path.
                expect(page.locator('#artifactFiles')).to_contain_text('校园日历.ics')
                with page.expect_download() as download:page.locator('#artifactFiles button').first.click()
                download.value.save_as(out/'calendar.ics');assert 'BEGIN:VEVENT' in (out/'calendar.ics').read_text(encoding='utf-8')
                page.locator('[data-view=digest]').click();page.screenshot(path=str(out/'desktop.png'))
                page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(out/'mobile.png'))
                assert not errors,errors
                browser.close()
            response=httpx.post(origin+'/api/shutdown',headers=headers,json={},trust_env=False);response.raise_for_status();process.wait(timeout=20)
            (out/'result.json').write_text(json.dumps(dict(ui=True,preset_preview_no_write=True,app_only_preview_report=True,worker_resume_and_pause=True,background_inbox_updates=True,authorization=True,mark_read=True,calendar_download=True,form_preserved=True,mobile=True,js_errors=errors,school_network_used=False),ensure_ascii=False),encoding='utf-8')
            print('Digest desktop acceptance passed.')
        finally:
            if process.poll() is None:process.terminate();process.wait(timeout=15)

if __name__=='__main__':main()
