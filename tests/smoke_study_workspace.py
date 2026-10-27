"""Native Tauri/Rust acceptance using synthetic school snapshots, no school writes."""
import asyncio, hashlib, json, os, socket, subprocess, tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
import threading
from http.server import ThreadingHTTPServer
from smoke_desktop import Model
from test_campus_pages import SCHEDULE, SEMESTERS
import httpx
from playwright.async_api import async_playwright, expect
from school_mcp.desktop.storage import Data, write_private, read_private
from school_mcp.bb.identity import save_session_identity

ROOT=Path(__file__).resolve().parents[1]
EXE=Path(os.environ.get('SCHOOL_STUDY_TEST_EXE',str(ROOT/'native/target/debug/ustc-school-desktop.exe')))
OUT=ROOT/'.local/study-workspace-evidence'
def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

class ModelServer(ThreadingHTTPServer):
    def __exit__(self, *args):
        self.shutdown()
        return super().__exit__(*args)

class WorkspaceModel(Model):
    seen_context=False
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        def call(args,seq):
            return {'role':'assistant','content':None,'tool_calls':[{'type':'function','id':'workspace_'+str(seq),'function':{'name':'school_workspace','arguments':json.dumps(args)}}]}
        results=[json.loads(m['content']) for m in body['messages'] if m['role']=='tool']
        if any(t['function']['name']=='connection_check' for t in body.get('tools',[])):
            msg=({'role':'assistant','content':results[-1]['verification_code']} if results else {'role':'assistant','content':None,'tool_calls':[{'type':'function','id':'conn','function':{'name':'connection_check','arguments':'{"ok":true}'}}]})
        else:
            WorkspaceModel.seen_context=any('用户在课程工作台选中的对象' in str(m.get('content','')) and 'bb:_1_1:_2_1' in str(m.get('content','')) for m in body['messages'] if m['role']=='system')
            if not results:msg=call({'action':'list'},1)
            elif len(results)==1:
                v=results[0]
                msg=call({'action':'update','id':'bb:_1_1:_2_1','revision':v['revision'],'checklist':[{'text':'提交 PDF 报告和代码','done':False,'source':'原文：请提交 PDF 报告和代码。'}]},2)
            else:msg={'role':'assistant','content':'已保存交付清单。'}
        raw=json.dumps({'choices':[{'message':msg}]},ensure_ascii=False).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)

async def run():
    OUT.mkdir(exist_ok=True);errors=[];checks=[]
    with tempfile.TemporaryDirectory(prefix='school-study-native-') as tmp, ModelServer(('127.0.0.1',0),WorkspaceModel) as model:
        threading.Thread(target=model.serve_forever,daemon=True).start()
        data=Data(tmp);save_session_identity('synthetic-study')
        write_private(data.profile/'timetable-view.dpapi',{'account':'synthetic-study','updated_at':'2026-10-23T10:00:00+08:00','data':SCHEDULE,'semesters':SEMESTERS['semesters']})
        write_private(data.profile/'course-workbench.dpapi',{'owner':hashlib.sha256(b'synthetic-study').hexdigest(),'revision':0,'updated_at':'2026-10-23T10:00:00+08:00','courses':[
            {'id':'_1_1','title':'2026FA 统计学（合成测试）','path':'/webapps/blackboard/execute/launcher?type=Course&id=_1_1','pinned':False,'personal_materials':[], 'announcements':{'text':'请阅读第一章。','url':'https://www.bb.ustc.edu.cn/'},'checked_at':'2026-10-23T10:00:00+08:00'}],
            'tasks':[{'id':'bb:_1_1:_2_1','course_id':'_1_1','content_id':'_2_1','source':'bb','title':'合成作业：实验报告','description':'请提交 PDF 报告和代码。','due_text':['截止时间尚未公布'],'progress':'not_started','platform_state':'not_checked','verification_error':{'code':'timeout','message':'BB 作业历史页响应超时。已保留上次核实记录；可稍后点击“核实提交记录”重试，或打开原作业页面。'},'notes':'','checklist':[], 'entry_path':'/webapps/assignment/uploadAssignment?course_id=_1_1&content_id=_2_1&mode=view'}]})
        def launch():
            p=port();proc=subprocess.Popen([str(EXE),'--data-dir',tmp,'--hidden'],env=dict(os.environ,SCHOOL_MAIN_TEST_CDP=str(p),WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f'--remote-debugging-port={p}'),creationflags=subprocess.CREATE_NO_WINDOW);return proc,p
        async def connect(pw,p):
            async with httpx.AsyncClient(trust_env=False,timeout=1) as h:
                for _ in range(200):
                    try:
                        if (await h.get(f'http://127.0.0.1:{p}/json/version')).is_success:break
                    except httpx.HTTPError:pass
                    await asyncio.sleep(.25)
            browser=await pw.chromium.connect_over_cdp(f'http://127.0.0.1:{p}');page=browser.contexts[0].pages[0]
            page.on('pageerror',lambda x:errors.append(str(x)))
            for _ in range(120):
                try:
                    if await page.evaluate('typeof Workbench!=="undefined" && loggedIn()'):break
                except Exception:pass
                await asyncio.sleep(.5)
            else: raise AssertionError('App not ready: '+str(errors))
            cd=await page.context.new_cdp_session(page);await cd.send('Emulation.setDeviceMetricsOverride',{'width':1440,'height':1000,'deviceScaleFactor':1,'mobile':False})
            return page,cd
        async with async_playwright() as pw:
            proc,p=launch()
            try:
                page,cd=await connect(pw,p)
                await page.locator('[data-view="courses"]').click();await page.get_by_role('button',name='进入课程',exact=True).wait_for()
                await page.screenshot(path=str(OUT/'courses.png'))
                await page.get_by_role('button',name='进入课程',exact=True).click();await page.get_by_role('button',name='问这门课',exact=True).wait_for()
                await page.get_by_label('关联教务课程',exact=True).select_option('合成课程')
                await expect(page.locator('#courseWorkspace')).to_contain_text('08:00–09:40')
                await page.get_by_label('关联教务课程',exact=True).select_option('合成日期待核对')
                await expect(page.locator('#courseWorkspace .study-overview')).to_contain_text('日期待核对 · 时间待核对')
                checks.append('timetable_mapping_and_unknown_date')
                await page.get_by_role('button',name='问这门课',exact=True).click()
                assert await page.locator('#studyAssistant').is_visible()
                assert await page.locator('#messageInput').is_visible()
                assert await page.evaluate('Workbench.context.kind')=='course'
                await page.screenshot(path=str(OUT/'course-assistant.png'))
                await page.locator('#studyAssistantClose').click();checks.append('course_context_side_assistant')
                await page.locator('[data-view="assignments"]').click();await page.locator('.study-task-row').first.wait_for()
                await page.locator('.study-task-row').first.click()
                await expect(page.locator('.study-verification-error')).to_contain_text('已保留上次核实记录')
                await page.get_by_label('我的进度',exact=True).select_option('completed')
                await page.get_by_label('个人备注',exact=True).fill('准备完成，等待提交')
                await page.get_by_role('button',name='核实提交记录',exact=True).click()
                assert not (await page.evaluate("api('/api/status')"))['busy']
                await expect(page.get_by_label('个人备注',exact=True)).to_have_value('准备完成，等待提交')
                checks.append('failed_history_guidance_and_unsaved_edit_protection')
                await page.get_by_role('button',name='+ 清单事项',exact=True).click()
                await page.get_by_placeholder('需要完成的事项').fill('准备 PDF 报告')
                await page.get_by_placeholder('原文摘录、页码，或“个人建议”').fill('原文：请提交 PDF 报告和代码。')
                await page.get_by_label('此项已完成').check()
                await page.get_by_role('button',name='保存任务',exact=True).click()
                for _ in range(60):
                    if await page.evaluate('!Workbench.dirty'):break
                    await asyncio.sleep(.1)
                state=await page.evaluate("api('/api/study')")
                assert state['tasks'][0]['progress']=='completed' and state['tasks'][0]['platform_state']=='not_checked'
                assert state['tasks'][0]['checklist'][0]['done'];checks.append('progress_checklist_saved_submission_unchanged')
                await page.screenshot(path=str(OUT/'task-detail.png'))
                await page.get_by_role('button',name='关闭详情',exact=True).click()
                await page.locator('[data-view="settings"]').click()
                await page.locator('#apiBase').fill(f'http://127.0.0.1:{model.server_port}/v1')
                await page.locator('#apiModel').fill('synthetic-model');await page.locator('#apiKey').fill('synthetic-key');await page.locator('#saveModel').click()
                for _ in range(120):
                    if '测试已通过' in await page.locator('#modelStatus').inner_text():break
                    await asyncio.sleep(.25)
                else:raise AssertionError('model connection test failed')
                # A historical operation error must disappear once recovery succeeds;
                # unrelated editor errors must remain visible.
                await page.evaluate("showError('合成历史登录错误','old-operation');lastOperation='';refresh()")
                await expect(page.locator('#actionFeedback')).to_be_hidden()
                await page.evaluate("showError('合成表单错误');lastOperation='';refresh()")
                await expect(page.locator('#actionFeedback')).to_be_visible()
                await page.evaluate('clearFeedback()');checks.append('recovered_operation_clears_stale_banner_only')
                await page.locator('[data-view="assignments"]').click()
                await page.get_by_role('button',name='全部',exact=True).click()
                await page.locator('.study-task-row').first.click()
                await page.get_by_role('button',name='让助手整理要求',exact=True).click()
                await page.locator('#executePermit').uncheck()
                await page.locator('#send').click()
                await page.locator('#approve').wait_for(state='visible',timeout=45000)
                assert WorkspaceModel.seen_context,'selected task absent from model context'
                before=await page.evaluate("api('/api/study')")
                assert before['tasks'][0]['checklist'][0]['text']=='准备 PDF 报告','model write bypassed approval'
                assert '提交 PDF 报告和代码' in await page.locator('#approvalArgs').text_content(), await page.locator('#approvalArgs').text_content()
                await page.locator('#approve').click()
                for _ in range(120):
                    if '已保存交付清单' in await page.locator('#messages').inner_text():break
                    await asyncio.sleep(.25)
                else:raise AssertionError('workspace assistant did not finish')
                saved=await page.evaluate("api('/api/study')")
                assert saved['tasks'][0]['checklist'][0]['text']=='提交 PDF 报告和代码'
                assert saved['tasks'][0]['platform_state']=='not_checked'
                chats=await page.evaluate("api('/api/chats')")
                assert chats['chats'][0]['study_context']=={'kind':'task','id':'bb:_1_1:_2_1'}
                await page.screenshot(path=str(OUT/'task-assistant.png'))
                await page.locator('#studyAssistantClose').click()
                await page.get_by_role('button',name='继续对话：',exact=False).wait_for()
                checks.append('contextual_assistant_tool_approval_and_related_history')
                await page.get_by_role('button',name='关闭详情',exact=True).click()
                await page.get_by_role('button',name='+ 添加任务',exact=True).click()
                await page.get_by_label('任务名称',exact=True).fill('合成手动阅读任务')
                await page.get_by_label('任务要求',exact=True).fill('阅读第一章并记笔记。')
                await page.locator('dialog.study-dialog').get_by_role('button',name='保存',exact=True).click();await page.locator('dialog.study-dialog').wait_for(state='detached')
                await page.get_by_role('button',name='合成手动阅读任务',exact=False).click()
                await page.get_by_role('button',name='加入日程',exact=True).click()
                start=(datetime.now(timezone(timedelta(hours=8)))+timedelta(days=1)).replace(second=0,microsecond=0)
                await page.get_by_label('开始时间（北京时间）',exact=True).fill(start.strftime('%Y-%m-%dT%H:%M'))
                await page.get_by_label('结束时间（北京时间）',exact=True).fill((start+timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'))
                await page.get_by_role('button',name='创建日程',exact=True).click();await page.locator('dialog.study-dialog').wait_for(state='detached')
                await page.get_by_role('button',name='日程：',exact=False).wait_for();checks.append('manual_task_and_linked_schedule')
                await page.get_by_role('button',name='日程：',exact=False).click()
                await page.get_by_role('button',name='返回关联任务',exact=True).click()
                await expect(page.locator('#studyTaskDetail h2')).to_have_text('合成手动阅读任务')
                checks.append('schedule_returns_to_linked_task')
                await page.get_by_role('button',name='归档任务',exact=True).click();await page.get_by_role('button',name='恢复任务',exact=True).wait_for()
                await page.get_by_role('button',name='恢复任务',exact=True).click();await page.get_by_role('button',name='归档任务',exact=True).wait_for();checks.append('archive_restore')
                await page.get_by_role('button',name='关闭详情',exact=True).click()
                await cd.send('Emulation.setDeviceMetricsOverride',{'width':430,'height':900,'deviceScaleFactor':1,'mobile':False});await page.screenshot(path=str(OUT/'mobile-width.png'))
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth+2');checks.append('narrow_layout_no_horizontal_overflow')
                state=await page.evaluate("api('/api/study')")
                stale=await page.evaluate("async()=>{try{await api('/api/study',{action:'update',revision:0,id:'bb:_1_1:_2_1',progress:'not_started'});return false}catch{return true}}")
                assert stale;checks.append('stale_edit_rejected')
                await page.evaluate("api('/api/shutdown',{})")
            finally:
                try:proc.wait(12)
                except subprocess.TimeoutExpired:proc.terminate();proc.wait(8)
            proc,p=launch()
            try:
                page,cd=await connect(pw,p);after=await page.evaluate("api('/api/study')")
                assert state==after;checks.append('restart_persistence')
                assert not errors,errors
                await page.evaluate("api('/api/shutdown',{})")
            finally:
                try:proc.wait(12)
                except subprocess.TimeoutExpired:proc.terminate();proc.wait(8)
    result={'checks':checks,'page_errors':errors,'runtime':'real Tauri/WebView2 + Rust IPC','data':'synthetic; no school network'}
    (OUT/'native-smoke.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':asyncio.run(run())
