"""Headless source/frozen page acceptance, synthetic account, no model or school network."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from unittest.mock import Mock,patch
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import sync_playwright,expect
from school_mcp.desktop import campus
from school_mcp.desktop.app import Application
from school_mcp.desktop.server import Server
from school_mcp.desktop.storage import read_private,write_private
from school_mcp.bb.identity import save_session_identity
from test_campus_pages import SCHEDULE,SEMESTERS


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--exe');args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];out=root/'.local/campus-pages';out.mkdir(exist_ok=True)
    kind='exe' if args.exe else 'source';errors=[];network=[]
    with tempfile.TemporaryDirectory(prefix='school-campus-smoke-') as tmp:
        app=Application(tmp);save_session_identity('synthetic-account')
        fixture=Mock();fixture.semesters.return_value=SEMESTERS;fixture.timetable.return_value=SCHEDULE
        with patch.object(campus,'JWClient',return_value=fixture):campus.refresh_timetable(app.data)
        process=server=None
        try:
            if args.exe:
                process=subprocess.Popen([str(Path(args.exe).resolve()),'--data-dir',tmp,'--no-open'],cwd=root,creationflags=subprocess.CREATE_NO_WINDOW)
                for _ in range(200):
                    if (app.data.root/'instance.dpapi').exists():break
                    if process.poll() is not None:raise AssertionError('EXE startup failed')
                    time.sleep(.2)
                url=read_private(app.data.root/'instance.dpapi')['url']
            else:
                server=Server(app);threading.Thread(target=server.serve_forever,daemon=True).start();url=server.origin+'/#'+server.token
            parsed=urlsplit(url);origin=parsed.scheme+'://'+parsed.netloc
            api=httpx.Client(base_url=origin,headers={'X-School-Token':parsed.fragment,'Origin':origin},trust_env=False,timeout=15)
            with patch.object(campus,'JWClient',return_value=fixture),patch('school_mcp.desktop.model.ModelClient.complete',side_effect=AssertionError('unexpected model')) as model:
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='chrome',headless=True)
                    context=browser.new_context(viewport={'width':1440,'height':1000})
                    def guard(route):
                        if urlsplit(route.request.url).hostname=='127.0.0.1':return route.continue_()
                        network.append(route.request.url)
                        assert route.request.url=='https://jw.ustc.edu.cn/home'
                        assert 'x-school-token' not in route.request.headers
                        route.fulfill(body='<html><title>synthetic original site</title></html>',content_type='text/html')
                    context.route('**/*',guard)
                    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url)
                    expect(page.locator('#sideIdentity')).to_have_text('synthetic-account',timeout=10000)
                    page.locator('[data-view=sites]').click();expect(page.locator('#siteGrid a')).to_have_count(17)
                    page.locator('#siteSearch').fill('教务系统');expect(page.locator('#siteGrid a')).to_have_count(1)
                    with context.expect_page() as opened:page.locator('#siteGrid a').click()
                    popup=opened.value;popup.wait_for_load_state();assert popup.url=='https://jw.ustc.edu.cn/home'
                    assert popup.evaluate('window.opener===null && document.referrer===""');popup.close()
                    page.locator('#siteSearch').fill('');page.screenshot(path=str(out/f'{kind}-sites.png'))
                    page.locator('[data-view=profile]').click();expect(page.locator('#profile-name')).to_have_value('')
                    page.locator('#profile-name').fill('synthetic-private-name');page.locator('#profile-email').fill('synthetic@example.test')
                    page.wait_for_timeout(3300);expect(page.locator('#profile-name')).to_have_value('synthetic-private-name')
                    page.locator('#saveProfile').click();expect(page.locator('#profileStatus')).to_contain_text('已保存到本机')
                    page.reload();page.locator('[data-view=profile]').click();expect(page.locator('#profile-name')).to_have_value('synthetic-private-name')
                    page.locator('#profile-name').fill('synthetic-unsaved');page.locator('#reloadProfile').click()
                    page.locator('#confirmDialog').get_by_role('button',name='取消',exact=True).click()
                    expect(page.locator('#profile-name')).to_have_value('synthetic-unsaved')
                    page.screenshot(path=str(out/f'{kind}-profile.png'))
                    page.locator('[data-view=timetable]').click();expect(page.locator('#timetableBoard')).to_contain_text('合成课程')
                    expect(page.locator('#timetableUnplaced')).to_contain_text('合成日期待核对')
                    expect(page.locator('#timetableUnplaced')).to_contain_text('含自定义日期')
                    expect(page.locator('#timetableBoard')).not_to_contain_text('合成其他课程')
                    page.locator('#timetableWeek').select_option('3');expect(page.locator('#timetableBoard')).to_contain_text('合成其他课程')
                    page.locator('#timetableSemester').select_option('43');expect(page.locator('#timetableStatus')).to_contain_text('已切换学期')
                    expect(page.locator('#timetableBoard .lesson-card')).to_have_count(0)
                    page.locator('#timetableSemester').select_option('42')
                    with page.expect_response('**/api/campus/timetable/refresh') as started:page.locator('#refreshTimetable').click()
                    started.value.json()
                    for _ in range(100):
                        operation=api.get('/api/status').json()['operation']
                        if operation.get('kind')=='campus_timetable' and operation['state']!='running':break
                        time.sleep(.1)
                    if args.exe:
                        expect(page.locator('#timetableStatus')).to_contain_text('当前仍显示上次成功刷新的快照',timeout=20000)
                    else:
                        expect(page.locator('#refreshTimetable')).to_be_enabled(timeout=20000)
                        expect(page.locator('#timetableStatus')).to_contain_text('快照更新于',timeout=20000)
                        assert fixture.timetable.call_count>=2
                    page.screenshot(path=str(out/f'{kind}-timetable.png'))
                    page.set_viewport_size({'width':390,'height':844})
                    for view in ['sites','timetable','profile']:
                        page.locator('[data-view='+view+']').click()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),view
                        page.screenshot(path=str(out/f'{kind}-{view}-mobile.png'))
                    # Lock removes private contents from the DOM and rejects APIs.
                    (app.data.root/'locked').touch();expect(page.locator('#profile-name')).to_have_count(0,timeout=10000)
                    expect(page.locator('#timetableBoard')).to_be_empty()
                    assert api.get('/api/campus/profile').status_code==400
                    assert api.get('/api/campus/timetable').status_code==400
                    assert not api.get('/api/chats').is_success
                    model.assert_not_called();assert not errors,errors
                    browser.close()
            assert len(network)==1
            assert b'synthetic-private-name' not in (app.data.profile/'personal-profile.dpapi').read_bytes()
            evidence={'target':kind,'navigation_opens_original':True,'17_entries':True,'no_token_or_opener_leak':True,
                'profile_persists_encrypted':True,'editing_survives_poll':True,'week_semester_filters':True,
                'lock_clears_private_dom':True,'mobile_no_overflow':True,'model_calls':0,'real_school_calls':0,
                'refresh_test':'auth_failure_keeps_cache' if args.exe else 'synthetic_school_success'}
            (out/f'{kind}.json').write_text(json.dumps(evidence),encoding='utf-8');print(json.dumps(evidence))
        finally:
            if server:server.shutdown();server.server_close()
            if process:
                try:api.post('/api/shutdown',json={});process.wait(20)
                finally:
                    if process.poll() is None:process.terminate();process.wait(10)

if __name__=='__main__':main()
