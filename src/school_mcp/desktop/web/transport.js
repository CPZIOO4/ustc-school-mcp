'use strict';
// One UI, two transports. Native credentials never enter the WebView.
window.schoolFetch = async function(path, options = {}) {
  // Only local JSON reads may time out here. A timed-out write has an unknown
  // outcome, so never turn it into an apparently failed/retryable submission.
  const boundedRead = (options.method || 'GET') === 'GET' && path.startsWith('/api/') && !path.startsWith('/api/artifact?');
  const controller = boundedRead && !window.__TAURI__ && !options.signal ? new AbortController() : null;
  let timer;
  const request = async () => {
  if (!window.__TAURI__) {
    const response = await fetch(path, controller ? {...options, signal: controller.signal} : options);
    if (!boundedRead) return response;
    // fetch resolves on headers; include the body in the read deadline too.
    const bytes = await response.arrayBuffer();
    return new Response([204,205,304].includes(response.status) ? null : bytes,
      {status: response.status, statusText: response.statusText, headers: response.headers});
  }
  const headers = {'x-school-route': path, 'x-school-method': options.method || 'GET'};
  const name = new Headers(options.headers).get('X-File-Name');
  if (name) headers['x-school-file'] = name;
  const body = options.body instanceof Blob ? new Uint8Array(await options.body.arrayBuffer())
    : new TextEncoder().encode(options.body || '');
  const raw = await window.__TAURI__.core.invoke('backend_request', body, {headers});
  const bytes = new Uint8Array(raw);
  if (bytes.length < 2) throw Error('本机服务返回不完整，请重新打开应用。');
  return new Response(bytes.slice(2), {status: (bytes[0] << 8) | bytes[1],
    headers: {'Content-Type': path.startsWith('/api/artifact?') ? 'application/octet-stream' : 'application/json'}});
  };
  if (!boundedRead) return request();
  try {
    return await Promise.race([request(), new Promise((_, reject) => {
      timer = setTimeout(() => {
        reject(Error('本机读取超时。后台任务可能仍在运行，请先核对任务状态。'));
        controller?.abort();
      }, 20000);
    })]);
  } finally {clearTimeout(timer);}
};
if (window.__TAURI__) {
  let linkMenu;
  const closeLinkMenu=()=>{linkMenu?.remove();linkMenu=null;};
  const notice=error=>window.dispatchEvent(new CustomEvent('native-notice',{detail:typeof error==='string'?error:'此链接无法打开，请检查网址。'}));
  document.addEventListener('contextmenu',event=>{
    const link=event.target.closest?.('a[href]');
    if(!link||!/^https?:/i.test(link.getAttribute('href')))return;
    event.preventDefault();closeLinkMenu();
    const menu=document.createElement('div');menu.className='link-context-menu';menu.setAttribute('role','menu');
    const open=document.createElement('button');open.type='button';open.setAttribute('role','menuitem');open.textContent='在新标签页中打开';
    open.onclick=()=>{closeLinkMenu();window.__TAURI__.core.invoke('browser_open',{url:link.href,background:true}).catch(notice);};
    menu.append(open);document.body.append(menu);linkMenu=menu;
    menu.style.left=Math.max(8,Math.min(event.clientX,innerWidth-menu.offsetWidth-8))+'px';
    menu.style.top=Math.max(8,Math.min(event.clientY,innerHeight-menu.offsetHeight-8))+'px';open.focus();
  });
  document.addEventListener('pointerdown',event=>{if(linkMenu&&!linkMenu.contains(event.target))closeLinkMenu();});
  document.addEventListener('keydown',event=>{if(event.key==='Escape')closeLinkMenu();});
  window.addEventListener('blur',closeLinkMenu);window.addEventListener('resize',closeLinkMenu);
  document.addEventListener('scroll',closeLinkMenu,true);
  document.addEventListener('click', event => {
    const link = event.target.closest?.('a[href]');
    if (!link || !/^https?:/i.test(link.getAttribute('href'))) return;
    event.preventDefault();
    window.__TAURI__.core.invoke('browser_open', {url: link.href}).catch(error => {
      window.dispatchEvent(new CustomEvent('native-notice', {detail: typeof error==='string'?error:'此链接无法打开，请检查网址。'}));
    });
  });
  window.__TAURI__.event.listen('native-notice', e => {
    window.dispatchEvent(new CustomEvent('native-notice', {detail: e.payload}));
  });
}
