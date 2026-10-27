"""Real native WebView2 tab/session/isolation smoke. Assertions use synthetic HTML/cookies; the initial HTTPS navigation may reach the official host."""
import os,socket,subprocess,tempfile,time,json
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright,expect
from school_mcp.desktop.storage import Data,write_private
from school_mcp.bb.identity import save_session_identity

def main():
 with tempfile.TemporaryDirectory(prefix='school-tabs-') as tmp:
  data=Data(tmp);save_session_identity('tabs-fixture')
  write_private(data.profile/'jw.session.dpapi',{'base_url':'https://jw.ustc.edu.cn','user_agent':'fixture','account':'tabs-fixture','cookies':[{'name':'session','value':'synthetic-only','domain':'jw.ustc.edu.cn','path':'/','secure':True,'httpOnly':True,'expires':time.time()+3600,'sameSite':'Lax'}],'origins':[]})
  with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
  with socket.socket() as s:s.bind(('127.0.0.1',0));child_port=s.getsockname()[1]
  proc=subprocess.Popen([str(Path('native/target/debug/ustc-school-desktop.exe').resolve()),'--data-dir',tmp,'--hidden'],env=dict(os.environ,SCHOOL_MAIN_TEST_CDP=str(port),SCHOOL_BROWSER_TEST_CDP=str(child_port)),creationflags=subprocess.CREATE_NO_WINDOW)
  try:
   with httpx.Client(trust_env=False,timeout=1) as c:
    for _ in range(150):
     try:
      if c.get(f'http://127.0.0.1:{port}/json/version').is_success:break
     except httpx.HTTPError:pass
     time.sleep(.2)
   with sync_playwright() as pw:
    br=pw.chromium.connect_over_cdp(f'http://127.0.0.1:{port}');ctx=br.contexts[0];page=ctx.pages[0];errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    expect(page.locator('#sideIdentity')).to_have_text('tabs-fixture',timeout=60000)
    invoke=lambda command,args:page.evaluate('(a)=>window.__TAURI__.core.invoke(a[0],a[1])',[command,args])
    invoke('browser_open',{'url':'https://jw.ustc.edu.cn/_school_tab_fixture'})
    expect(page.locator('.browser-tab')).to_have_count(1,timeout=15000)
    child_br=pw.chromium.connect_over_cdp(f'http://127.0.0.1:{child_port}');child_ctx=child_br.contexts[0];child=child_ctx.pages[0]
    child_ctx.route('**/*',lambda r:r.fulfill(content_type='text/html',body='<title>教务测试页</title><h1>Native tab</h1><a target="_blank" href="https://jw.ustc.edu.cn/second">Open tab</a>'))
    child.goto('https://jw.ustc.edu.cn/home');expect(child.locator('h1')).to_have_text('Native tab')
    cookies=child_ctx.cookies('https://jw.ustc.edu.cn');assert any(c['name']=='session' and c['value']=='synthetic-only' for c in cookies),[(c['name'],c['domain']) for c in cookies]
    denied=child.evaluate("async()=>{try{await window.__TAURI__.core.invoke('browser_action',{action:'state'});return false}catch{return true}}")
    assert denied,'Remote page reached native commands'
    try:page.evaluate("async()=>await schoolFetch('/api/native-browser-session?host=jw.ustc.edu.cn')");raise AssertionError('Session endpoint exposed')
    except Exception as e:assert 'Session endpoint exposed' not in str(e)
    child.locator('a').click();expect(page.locator('.browser-tab')).to_have_count(2,timeout=15000)
    page.locator('.browser-home').click();expect(page.locator('#messageInput')).to_be_visible()
    page.locator('.browser-tab [role=tab]').first.click();expect(page.locator('body')).to_have_class(__import__('re').compile('.*browsing.*'))
    page.locator('.browser-close').first.click();expect(page.locator('.browser-tab')).to_have_count(1)
    page.evaluate("async()=> (await schoolFetch('/api/clear',{method:'POST',body:JSON.stringify({scope:'logout',confirmation:'logout'})})).json()")
    expect(page.locator('.browser-tab')).to_have_count(0)
    try:invoke('browser_open',{'url':'https://jw.ustc.edu.cn/home'});raise AssertionError('Locked browser opened')
    except Exception as e:assert 'Locked browser opened' not in str(e)
    assert not errors,errors
    page.evaluate("async()=> (await schoolFetch('/api/shutdown',{method:'POST',body:'{}'})).json()")
    print(json.dumps({'native_tabs':True,'cookie_import':True,'remote_ipc_denied':True,'popup_tab':True,'lock_closes_tabs':True}))
  finally:
   if proc.poll() is None:
    try:proc.wait(8)
    except subprocess.TimeoutExpired:proc.terminate();proc.wait(10)
if __name__=='__main__':main()
