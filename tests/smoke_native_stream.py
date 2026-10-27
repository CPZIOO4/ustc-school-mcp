"""Real native WebView2 + local SSE: incremental rendering and shared send/stop slot."""
import json,os,socket,subprocess,tempfile,threading,time,argparse
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import httpx
from playwright.sync_api import sync_playwright,expect
from school_mcp.desktop.storage import Data,write_private
from school_mcp.bb.identity import save_session_identity
class Model(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));assert body['stream'] is True
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Connection','close');self.end_headers()
  try:
   for part in ['\n','这是','真实','分块','到达的','回复。']:
    self.wfile.write(('data: '+json.dumps({'choices':[{'index':0,'delta':{'content':part},'finish_reason':None}]})+'\n\n').encode());self.wfile.flush();time.sleep(.65)
   self.wfile.write(('data: '+json.dumps({'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})+'\n\ndata: [DONE]\n\n').encode());self.wfile.flush()
  except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass

def main():
 p=argparse.ArgumentParser();p.add_argument('--exe',required=True);args=p.parse_args()
 with tempfile.TemporaryDirectory(prefix='school-stream-') as tmp,ThreadingHTTPServer(('127.0.0.1',0),Model) as model:
  threading.Thread(target=model.serve_forever,daemon=True).start();data=Data(tmp);save_session_identity('stream-fixture')
  write_private(data.root/'model-settings.dpapi',{'base_url':f'http://127.0.0.1:{model.server_port}/v1','model':'fixture','api_key':'synthetic','tested':True})
  with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
  proc=subprocess.Popen([str(Path(args.exe).resolve()),'--data-dir',tmp,'--hidden'],env=dict(os.environ,WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f'--remote-debugging-port={port}'),creationflags=subprocess.CREATE_NO_WINDOW)
  try:
   with httpx.Client(trust_env=False,timeout=1) as c:
    for _ in range(150):
     try:
      if c.get(f'http://127.0.0.1:{port}/json/version').is_success:break
     except httpx.HTTPError:pass
     time.sleep(.2)
    else:raise AssertionError('Native startup failed')
   with sync_playwright() as pw:
    br=pw.chromium.connect_over_cdp(f'http://127.0.0.1:{port}');page=br.contexts[0].pages[0];errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    expect(page.locator('#sideIdentity')).to_have_text('stream-fixture',timeout=60000)
    page.locator('#messageInput').fill('请直接回复一句话');send=page.locator('#send').bounding_box();page.locator('#send').click()
    samples=[]
    for _ in range(65):
     page.evaluate('refresh()');text=page.locator('#messages').inner_text();samples.append(text)
     assert page.locator('#messages .message.assistant').evaluate_all('(els)=>els.every(e=>e.textContent.trim().length>0)')
     if page.locator('#stop').is_visible():
      stop=page.locator('#stop').bounding_box();assert abs(stop['x']-send['x'])<1 and abs(stop['width']-send['width'])<1
      assert page.locator('#send').is_hidden()
     if '真实分块到达的回复。' in text and page.locator('#stop').is_hidden():break
     time.sleep(.1)
    assert any('真实' in s and '回复。' not in s for s in samples),samples
    expect(page.locator('#messages .assistant')).to_have_text('这是真实分块到达的回复。')
    expect(page.locator('#messages .message.user')).to_have_text('请直接回复一句话')
    page.locator('#messageInput').fill('测试停止');page.locator('#send').click()
    for _ in range(20):
     page.evaluate('refresh()')
     if page.locator('#stop').is_visible():break
     time.sleep(.1)
    page.locator('#stop').click()
    for _ in range(30):
     page.evaluate('refresh()')
     if page.locator('#stop').is_hidden():break
     time.sleep(.1)
    expect(page.locator('#progress')).to_contain_text('已停下')
    expect(page.locator('#messages .assistant')).to_have_count(1)
    assert not errors,errors
    page.evaluate("async()=> (await schoolFetch('/api/shutdown',{method:'POST',body:'{}'})).json()")
    print(json.dumps({'native_streaming':True,'partial_samples':len(set(samples)),'shared_button_position':True,'stop_cancels':True,'page_errors':errors}))
  finally:
   if proc.poll() is None:
    try:proc.wait(10)
    except subprocess.TimeoutExpired:proc.terminate();proc.wait(10)
   model.shutdown()
if __name__=='__main__':main()
