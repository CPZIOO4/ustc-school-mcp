(() => {
  // Keep official login links in the single guarded page. Workers/popups retain
  // the default CDP deny policy; no extra renderer obtains unguarded credentials.
  Object.defineProperty(window, 'open', {value: url => { if(url) location.assign(String(url)); return window; }, configurable:false});
  document.addEventListener('click', event => { const a=event.target.closest?.('a[target]'); if(a) a.target='_self'; },true);
  document.addEventListener('submit', event => { event.target.target='_self'; },true);
  if(location.protocol!=='https:' || !['id.ustc.edu.cn','passport.ustc.edu.cn'].includes(location.hostname) || (location.port && location.port!=='443')) return;
  const report=()=>{
    const inputs=[...document.querySelectorAll('input[name="username"],input#username,input[placeholder*="学工号"],input[placeholder*="GID"]')].filter(e=>e.type!=='password');
    if(inputs.length===1 && /^[A-Za-z0-9_.@-]{1,100}$/.test(inputs[0].value)) window.schoolLoginAccount(inputs[0].value);
  };
  document.addEventListener('change',report,true);
  document.addEventListener('submit',report,true);
  document.addEventListener('click',event=>{if(event.target.closest?.('button,input[type="submit"]'))report();},true);
})();
