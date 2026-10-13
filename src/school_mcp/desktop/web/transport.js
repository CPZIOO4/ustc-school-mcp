'use strict';
// One UI, two transports. Native credentials never enter the WebView.
window.schoolFetch = async function(path, options = {}) {
  if (!window.__TAURI__) return fetch(path, options);
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
if (window.__TAURI__) {
  document.addEventListener('click', event => {
    const link = event.target.closest?.('a[href]');
    if (!link || !/^https?:/i.test(link.getAttribute('href'))) return;
    event.preventDefault();
    window.__TAURI__.core.invoke('open_external', {url: link.href}).catch(() => {
      window.dispatchEvent(new CustomEvent('native-notice', {detail: '此链接无法打开，请检查网址。'}));
    });
  });
  window.__TAURI__.event.listen('native-notice', e => {
    window.dispatchEvent(new CustomEvent('native-notice', {detail: e.payload}));
  });
}
