"""Offline real-Chrome checks for concrete desktop interaction regressions.

Run explicitly: SCHOOL_UX_BROWSER_TESTS=1 python -m unittest discover -s tests
-p test_ux_interactions.py. All routes are synthetic; no school/model calls.
"""
import json
import os
from pathlib import Path
import unittest
from urllib.parse import urlsplit, parse_qs

from playwright.sync_api import sync_playwright, expect

WEB = Path(__file__).resolve().parents[1] / 'src/school_mcp/desktop/web'


@unittest.skipUnless(os.environ.get('SCHOOL_UX_BROWSER_TESTS') == '1', 'opt-in headless Chrome interaction checks')
class InteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel='chrome', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.counts = {}
        self.sent = []
        self.account = 'synthetic-student'
        self.busy = False
        self.op = {'id': 'idle', 'kind': '', 'state': 'idle', 'detail': ''}
        self.chats = {name: {'id': name, 'state': 'completed', 'messages': [{'role': 'assistant', 'content': '对话 '+name}], 'artifacts': [], 'events': [], 'tasks': [], 'pending': None} for name in ['a', 'b']}
        self.page.route('**/*', self.route)
        self.page.goto('http://schoolagent.test/#synthetic-token')
        expect(self.page.locator('#sideIdentity')).to_have_text(self.account)
        expect(self.page.locator('#chatList button')).to_have_count(2)
        self.page.evaluate('clearTimeout(pollTimer)')

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def route(self, route):
        url = urlsplit(route.request.url)
        path = url.path
        self.counts[path] = self.counts.get(path, 0) + 1
        if path == '/api/status':
            value = {'identity': {'saved': True, 'account': self.account, 'email_verification': False}, 'locked': False,
                     'model': {'tested': True, 'configured': True, 'model': 'synthetic-model', 'base_url': 'https://model.invalid/v1'},
                     'model_providers': [{'id': 'custom', 'base_url': 'https://model.invalid/v1', 'model': 'synthetic-model'}],
                     'mail': {'configured': False}, 'chrome_installed': True, 'busy': self.busy, 'operation': self.op,
                     'data_location': 'synthetic-private-dir', 'services': []}
        elif path == '/api/chats':
            value = {'chats': [{'id': k, 'title': '对话 '+k, 'updated_at': '2026-10-18T10:00:00Z'} for k in self.chats]}
        elif path == '/api/chat':
            value = self.chats[parse_qs(url.query)['id'][0]]
        elif path == '/api/chat/send':
            self.sent.append(route.request.post_data_json)
            value = {'chat_id': route.request.post_data_json['chat_id'] or 'a'}
        elif path == '/api/chat/approve':
            self.chats[route.request.post_data_json['chat_id']]['pending'] = None
            value = {'cancelled': True}
        elif path == '/api/upload':
            value = {'name': '材料.txt', 'path': 'synthetic-upload', 'size_bytes': 5}
        elif path == '/api/digest':
            value = {'messages': [], 'items': [], 'source_health': {}, 'sources': [], 'worker_running': False,
                     'total_matched': 0, 'truncated': False, 'settings': {'enabled': False, 'sources': [], 'keywords': [],
                     'exclude': [], 'locations': [], 'interval_minutes': 60, 'daily_time': '10:00', 'urgent_hours': 24,
                     'max_pages': 1, 'detail_limit': 10, 'email': '', 'email_enabled': False, 'bb_courses': []}}
        elif path == '/api/diagnostics':
            value = {'available': False, 'note': '暂无记录'}
        elif path == '/api/stop':
            self.op = {'id': 'stop', 'kind': 'chat', 'state': 'running', 'stop_requested': True, 'detail': '等待当前请求结束'}
            value = {'ok': True}
        elif path.startswith('/api/'):
            value = {}
        else:
            asset = WEB / ('index.html' if path == '/' else path.lstrip('/'))
            if asset.is_file():
                route.fulfill(path=str(asset))
            else:
                route.fulfill(status=404, body='')
            return
        route.fulfill(content_type='application/json', body=json.dumps(value, ensure_ascii=False))

    def chat(self, name):
        self.page.locator('#chatList button').filter(has_text='对话 '+name).click()
        expect(self.page.locator('#messages')).to_contain_text('对话 '+name)

    def test_draft_text_and_attachments_stay_with_their_conversation(self):
        self.chat('a')
        self.page.locator('#messageInput').fill('A 未发送材料')
        self.page.locator('#fileInput').set_input_files({'name': '材料.txt', 'mimeType': 'text/plain', 'buffer': b'hello'})
        expect(self.page.locator('#fileChips')).to_contain_text('材料.txt')
        self.page.locator('#executePermit').check()
        self.chat('b')
        expect(self.page.locator('#messageInput')).to_have_value('')
        expect(self.page.locator('#fileChips')).to_have_text('')
        self.page.locator('#messageInput').fill('B 草稿')
        self.chat('a')
        expect(self.page.locator('#messageInput')).to_have_value('A 未发送材料')
        expect(self.page.locator('#fileChips')).to_contain_text('材料.txt')
        expect(self.page.locator('#executePermit')).not_to_be_checked()
        self.chat('b')
        expect(self.page.locator('#messageInput')).to_have_value('B 草稿')

    def test_ime_confirmation_does_not_send(self):
        self.chat('a')
        self.page.locator('#messageInput').fill('中文选词中')
        self.page.locator('#messageInput').dispatch_event('compositionstart')
        self.page.locator('#messageInput').dispatch_event('keydown', {'key': 'Enter', 'ctrlKey': True, 'isComposing': True})
        self.page.wait_for_timeout(100)
        self.assertEqual(self.sent, [])
        self.page.locator('#messageInput').dispatch_event('compositionend')
        self.page.locator('#messageInput').press('Control+Enter')
        expect(self.page.locator('#messageInput')).to_have_value('')
        self.assertEqual(len(self.sent), 1)

    def test_sending_response_does_not_steal_another_chat_or_draft(self):
        self.chat('a')
        self.page.evaluate("""window.originalFetch=schoolFetch;window.schoolFetch=(path,options)=>path==='/api/chat/send'?new Promise(resolve=>{window.finishSend=()=>resolve(new Response(JSON.stringify({chat_id:'a'}),{status:200}));}):originalFetch(path,options);void 0;""")
        self.page.locator('#messageInput').fill('发送 A')
        self.page.locator('#send').click()
        self.chat('b')
        self.page.locator('#messageInput').fill('保留 B 输入')
        self.page.evaluate('finishSend()')
        self.page.wait_for_function('!posting')
        expect(self.page.locator('#messageInput')).to_have_value('保留 B 输入')
        self.assertEqual(self.page.evaluate('activeChat'), 'b')
        self.chat('a')
        expect(self.page.locator('#messageInput')).to_have_value('')

    def test_text_typed_during_send_survives(self):
        self.chat('a')
        self.page.evaluate("""window.originalFetch=schoolFetch;window.schoolFetch=(path,options)=>path==='/api/chat/send'?new Promise(resolve=>{window.finishSend=()=>resolve(new Response(JSON.stringify({chat_id:'a'}),{status:200}));}):originalFetch(path,options);void 0;""")
        self.page.locator('#messageInput').fill('第一条')
        self.page.locator('#send').click()
        self.page.locator('#messageInput').fill('下一条补充')
        self.page.evaluate('finishSend()')
        self.page.wait_for_function('!posting')
        expect(self.page.locator('#messageInput')).to_have_value('下一条补充')

    def test_account_change_clears_old_conversation_and_drafts(self):
        self.chat('a')
        self.page.locator('#messageInput').fill('旧账号内容')
        self.account = 'other-synthetic-account'
        self.page.evaluate('refresh()')
        expect(self.page.locator('#messageInput')).to_have_value('')
        expect(self.page.locator('#messages')).to_have_text('')
        self.assertIsNone(self.page.evaluate('activeChat'))
        self.assertEqual(self.page.evaluate('composeDrafts.size'), 0)

    def test_idle_poll_does_not_rebuild_unchanged_chat_or_artifact_buttons(self):
        self.chats['a']['artifacts'] = [{'id': 'artifact', 'name': '结果.txt', 'size_bytes': 5}]
        self.chat('a')
        self.page.evaluate("window.originalArtifact=document.querySelector('#artifactFiles button')")
        count = self.counts['/api/chats']
        self.page.evaluate('refresh()')
        self.assertEqual(self.counts['/api/chats'], count)
        self.page.evaluate("loadChat('a')")
        self.assertTrue(self.page.evaluate("originalArtifact===document.querySelector('#artifactFiles button')"))

    def test_model_form_preserves_unsaved_input_when_switching_views(self):
        self.page.evaluate("show('settings')")
        self.page.locator('#apiModel').fill('unsaved-model')
        self.page.locator('#apiKey').fill('synthetic-key')
        self.page.evaluate("show('chat');show('settings')")
        expect(self.page.locator('#apiModel')).to_have_value('unsaved-model')
        expect(self.page.locator('#apiKey')).to_have_value('synthetic-key')

    def test_stop_request_remains_pending_and_cannot_repeat(self):
        self.busy = True
        self.op = {'id': 'running', 'kind': 'chat', 'state': 'running', 'detail': '正在处理'}
        self.chats['a']['state'] = 'running'
        self.chat('a')
        self.page.evaluate('refresh()')
        self.page.locator('#stop').click()
        expect(self.page.locator('#stop')).to_be_disabled()
        expect(self.page.locator('#progress')).to_contain_text('正在停止后续步骤')
        self.assertEqual(self.counts['/api/stop'], 1)

    def test_late_chat_response_cannot_replace_a_newer_response(self):
        self.chat('a')
        self.page.evaluate("""window.originalFetch=schoolFetch;window.responses=[];window.schoolFetch=(path,options)=>path.startsWith('/api/chat?')?new Promise(resolve=>responses.push(resolve)):originalFetch(path,options);loadChat('a');loadChat('a');void 0;""")
        self.page.evaluate("""responses[1](new Response(JSON.stringify({...currentChat,messages:[{role:'assistant',content:'新结果'}]}),{status:200}))""")
        expect(self.page.locator('#messages')).to_contain_text('新结果')
        self.page.evaluate("""responses[0](new Response(JSON.stringify({...currentChat,messages:[{role:'assistant',content:'旧结果'}]}),{status:200}))""")
        self.page.wait_for_timeout(50)
        expect(self.page.locator('#messages')).to_contain_text('新结果')

    def test_refresh_requested_during_poll_is_not_discarded(self):
        self.page.evaluate("""window.originalFetch=schoolFetch;window.statusReads=0;window.schoolFetch=(path,options)=>{if(path==='/api/status'){statusReads++;if(statusReads===1)return new Promise(resolve=>{window.finishStatus=async()=>resolve(await originalFetch(path,options));});}return originalFetch(path,options);};refresh();refresh();void 0;""")
        self.page.evaluate('finishStatus()')
        self.page.wait_for_function('refreshPromise===null')
        self.assertEqual(self.page.evaluate('statusReads'), 2)

    def test_polling_new_messages_does_not_jump_while_reading_history(self):
        self.chats['a']['messages'] = [{'role': 'assistant', 'content': ('对话 a 历史消息 '+str(i)+'\n')*4} for i in range(30)]
        self.chat('a')
        self.page.evaluate("document.getElementById('conversationScroll').scrollTo({top:100,behavior:'instant'})")
        before = self.page.locator('#conversationScroll').evaluate('(el)=>el.scrollTop')
        self.chats['a']['messages'].append({'role': 'assistant', 'content': '新消息'})
        self.page.evaluate("loadChat('a')")
        after = self.page.locator('#conversationScroll').evaluate('(el)=>el.scrollTop')
        self.assertAlmostEqual(before, after, delta=1)

    def test_quick_action_does_not_erase_existing_draft(self):
        self.page.locator('#messageInput').fill('我的补充条件')
        self.page.locator('[data-prompt]').first.click()
        expect(self.page.locator('#messageInput')).to_have_value('我的补充条件')
        expect(self.page.locator('#composerStatus')).to_contain_text('已保留')

    def test_new_chat_send_migrates_followup_draft_without_copying_it(self):
        self.page.evaluate("""window.originalFetch=schoolFetch;window.schoolFetch=(path,options)=>path==='/api/chat/send'?new Promise(resolve=>{window.finishSend=()=>resolve(new Response(JSON.stringify({chat_id:'a'}),{status:200}));}):originalFetch(path,options);void 0;""")
        self.page.locator('#messageInput').fill('新对话第一条')
        self.page.locator('#send').click()
        self.page.locator('#messageInput').fill('仅属于此对话的补充')
        self.page.evaluate('finishSend()')
        self.page.wait_for_function('!posting')
        expect(self.page.locator('#messageInput')).to_have_value('仅属于此对话的补充')
        self.page.locator('#newChat').click()
        expect(self.page.locator('#messageInput')).to_have_value('')
        self.chat('a')
        expect(self.page.locator('#messageInput')).to_have_value('仅属于此对话的补充')

    def test_narrow_history_keyboard_focus_returns_to_visible_control(self):
        self.page.set_viewport_size({'width':390,'height':700})
        self.page.locator('#historyToggle').focus()
        self.page.keyboard.press('Enter')
        self.assertTrue(self.page.evaluate("document.querySelector('#chatList').contains(document.activeElement)"))
        self.page.keyboard.press('Escape')
        expect(self.page.locator('#historyToggle')).to_be_focused()
        self.page.keyboard.press('Enter')
        self.page.keyboard.press('Enter')
        expect(self.page.locator('#messageInput')).to_be_focused()

    def test_transport_bounds_reads_but_never_times_out_submissions(self):
        result = self.page.evaluate("""async()=>{
          clearTimeout(pollTimer);
          const realTimer=window.setTimeout;window.setTimeout=(fn,ms,...args)=>realTimer(fn,ms===20000?20:ms,...args);
          let aborted=false;
          window.fetch=(path,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{aborted=true;reject(Error('aborted'));}));
          let readError='';try{await schoolFetch('/api/status')}catch(e){readError=e.message;}
          window.__TAURI__={core:{invoke:()=>new Promise(resolve=>realTimer(()=>resolve([0,200,...new TextEncoder().encode('{}')]),60))}};
          const write=await schoolFetch('/api/chat/send',{method:'POST',body:'{}'});
          return {aborted,readError,writeStatus:write.status};
        }""")
        self.assertTrue(result['aborted'])
        self.assertIn('读取超时', result['readError'])
        self.assertEqual(result['writeStatus'], 200)

    def test_follow_bottom_includes_all_downloads_after_messages_render(self):
        self.page.set_viewport_size({'width':390,'height':700})
        self.chats['a']['messages'] = [{'role': 'assistant', 'content': '对话 a 多条结果\n'*3} for _ in range(30)]
        self.chats['a']['artifacts'] = [{'id': str(i), 'name': '较长的结果文件名称'+str(i)+'.txt', 'size_bytes': 50} for i in range(10)]
        self.page.evaluate("selectChat('a');show('chat')")
        expect(self.page.locator('#artifactFiles button')).to_have_count(10)
        distance = self.page.locator('#conversationScroll').evaluate('(el)=>el.scrollHeight-el.clientHeight-el.scrollTop')
        self.assertLessEqual(abs(distance), 1)

    def test_transport_deadline_includes_a_stalled_response_body(self):
        error = self.page.evaluate("""async()=>{
          clearTimeout(pollTimer);
          const realTimer=window.setTimeout;window.setTimeout=(fn,ms,...args)=>realTimer(fn,ms===20000?20:ms,...args);
          window.fetch=async()=>new Response(new ReadableStream({start(controller){controller.enqueue(new TextEncoder().encode('{'));}}),{status:200,headers:{'Content-Type':'application/json'}});
          try{await schoolFetch('/api/status');return 'unexpected success';}catch(e){return e.message;}
        }""")
        self.assertIn('读取超时', error)

    def test_nonchat_operation_bar_exposes_stop_and_tracks_pending_state(self):
        self.busy = True
        for view, kind in [('settings','model_test'), ('accounts','login_identity')]:
            self.op = {'id':kind,'kind':kind,'state':'running','detail':'正在等待合成操作完成'}
            self.page.evaluate(f"show('{view}');void 0;")
            self.page.evaluate('refresh()')
            expect(self.page.locator('#operationBar')).to_be_visible()
            expect(self.page.locator('#operationDetail')).to_have_text('正在等待合成操作完成')
            expect(self.page.locator('#operationStop')).to_be_enabled()
        self.page.locator('#operationStop').click()
        expect(self.page.locator('#operationStop')).to_be_disabled()
        expect(self.page.locator('#operationDetail')).to_contain_text('等待当前请求结束')
        self.assertEqual(self.counts['/api/stop'], 1)
        self.page.evaluate("show('chat')")
        expect(self.page.locator('#operationBar')).not_to_be_visible()
        expect(self.page.locator('#stop')).to_be_disabled()
        self.busy = False
        self.page.evaluate("show('settings')")
        self.page.evaluate('refresh()')
        expect(self.page.locator('#operationBar')).not_to_be_visible()

    def test_cancel_approval_updates_immediately_without_operation_id_change(self):
        self.chats['a']['pending'] = {'id':'synthetic-approval','preview':{'subject':'合成操作'},'arguments':{}}
        self.chat('a')
        self.page.evaluate('refresh()')
        expect(self.page.locator('#approval')).to_be_visible()
        self.page.locator('#reject').click()
        expect(self.page.locator('#approval')).not_to_be_visible(timeout=1500)
        self.assertEqual(self.op['id'], 'idle')
        self.assertEqual(self.counts['/api/chat/approve'], 1)

    def test_unchanged_operation_poll_does_not_repeat_live_region_text(self):
        self.busy = True
        self.op = {'id':'model-test','kind':'model_test','state':'running','detail':'正在等待合成模型响应'}
        self.page.evaluate("show('settings');void 0;")
        self.page.evaluate('refresh()')
        self.page.evaluate("""window.liveMutations=[];window.liveObserver=new MutationObserver(records=>liveMutations.push(...records.map(r=>r.type)));liveObserver.observe(document.getElementById('operationBar'),{childList:true,characterData:true,subtree:true});void 0;""")
        self.page.evaluate('refresh()')
        self.assertEqual(self.page.evaluate('liveMutations'), [])
        self.op['detail'] = '正在核对工具调用结果'
        self.page.evaluate('refresh()')
        expect(self.page.locator('#operationDetail')).to_have_text('正在核对工具调用结果')
        self.assertGreater(len(self.page.evaluate('liveMutations')), 0)
        self.page.evaluate('liveObserver.disconnect()')

    def test_live_answer_drains_smoothly_after_final_and_history_is_immediate(self):
        self.chat('a')
        self.chats['a']['messages'] = []
        self.page.evaluate("loadChat('a')")
        self.chats['a']['streaming'] = {'content': '连续显示的文字。' * 15}
        self.page.evaluate("loadChat('a')")
        first = self.page.locator('#messages .assistant').inner_text()
        self.assertLess(len(first), 120)
        self.chats['a']['messages'] = [{'role':'assistant','content':'连续显示的文字。' * 15}]
        del self.chats['a']['streaming']
        self.page.evaluate("loadChat('a')")
        self.assertLess(len(self.page.locator('#messages .assistant').inner_text()), 120)
        expect(self.page.locator('#messages .assistant')).to_have_text('连续显示的文字。' * 15)
        self.chat('b')
        self.page.evaluate("selectChat('a');loadChat('a')")
        self.assertEqual(self.page.locator('#messages .assistant').inner_text(), '连续显示的文字。' * 15)

    def test_answer_links_are_safe_and_keep_source_text(self):
        self.chats['a']['messages'] = [{'role':'assistant','content':'原文：https://www.teach.ustc.edu.cn/notice/1.html。 [官网](https://www.ustc.edu.cn/) <img src=x onerror=alert(1)> javascript:alert(1) https://user:secret@example.com/'}]
        self.page.evaluate("selectChat('a');loadChat('a')")
        links=self.page.locator('#messages .assistant a')
        expect(links).to_have_count(2)
        self.assertEqual(links.first.get_attribute('href'),'https://www.teach.ustc.edu.cn/notice/1.html')
        self.assertEqual(links.first.get_attribute('target'),'_blank')
        expect(self.page.locator('#messages img')).to_have_count(0)
        expect(self.page.locator('#messages .assistant')).to_contain_text('<img src=x onerror=alert(1)>')

    def test_reduced_motion_skips_typewriter_and_switch_discards_old_queue(self):
        self.chat('a')
        self.chats['a']['messages'] = []
        self.page.evaluate("loadChat('a')")
        self.chats['a']['streaming'] = {'content':'尚未显示的文字' * 80}
        self.page.evaluate("loadChat('a')")
        self.chat('b')
        expect(self.page.locator('#messages .assistant')).to_have_text('对话 b')
        self.page.emulate_media(reduced_motion='reduce')
        self.page.evaluate("selectChat('a');loadChat('a')")
        self.assertEqual(self.page.locator('#messages .assistant').inner_text(),'尚未显示的文字' * 80)

    def test_failure_stays_visible_and_retains_input(self):
        self.chat('a')
        self.page.route('**/api/chat/send', lambda route: route.fulfill(status=503, content_type='application/json', body='{"error":"synthetic service unavailable"}'))
        self.page.locator('#messageInput').fill('保留请求')
        self.page.locator('#send').click()
        expect(self.page.locator('#actionFeedback')).to_be_visible()
        expect(self.page.locator('#messageInput')).to_have_value('保留请求')
        self.page.evaluate('refresh()')
        expect(self.page.locator('#actionFeedback')).to_contain_text('synthetic service unavailable')


if __name__ == '__main__':
    unittest.main()
