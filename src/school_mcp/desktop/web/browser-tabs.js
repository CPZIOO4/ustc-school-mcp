'use strict';
if(window.__TAURI__){
  const bar=document.createElement('div');bar.id='browserBar';bar.setAttribute('aria-label','网页标签');
  const tabs=document.createElement('div');tabs.className='browser-tabs';tabs.setAttribute('role','tablist');
  const controls=document.createElement('div');controls.className='browser-controls';
  const address=document.createElement('span');address.className='browser-address';
  let selected=null;
  const act=(action,id=selected)=>window.__TAURI__.core.invoke('browser_action',{action,id}).catch(e=>toast(String(e)));
  const btn=(label,fn)=>{const b=document.createElement('button');b.type='button';b.textContent=label;b.onclick=fn;return b;};
  const back=btn('←',()=>act('back'));back.title='后退';back.setAttribute('aria-label','后退');
  const forward=btn('→',()=>act('forward'));forward.title='前进';forward.setAttribute('aria-label','前进');
  const reload=btn('↻',()=>act('reload'));reload.title='刷新网页';reload.setAttribute('aria-label','刷新网页');
  controls.append(back,forward,reload,address);bar.append(tabs,controls);document.body.prepend(bar);document.body.classList.add('native-tabs');
  function render(state){
    selected=state.active;tabs.replaceChildren();
    const home=btn('学校助手',()=>act('chat'));home.className='browser-home';home.setAttribute('role','tab');home.setAttribute('aria-selected',String(!selected));tabs.append(home);
    for(const t of state.tabs){
      const wrap=document.createElement('div');wrap.className='browser-tab'+(selected===t.id?' active':'');
      const choose=btn((t.loading?'◌  ':'')+t.title,()=>act('select',t.id));choose.setAttribute('role','tab');choose.setAttribute('aria-selected',String(selected===t.id));choose.title=t.title;
      const close=btn('×',()=>act('close',t.id));close.className='browser-close';close.setAttribute('aria-label','关闭 '+t.title);wrap.append(choose,close);tabs.append(wrap);
    }
    const current=state.tabs.find(t=>t.id===selected);controls.classList.toggle('hidden',!current);
    if(current){try{const u=new URL(current.url);address.textContent=u.hostname+u.pathname;}catch{address.textContent='正在打开网页…';}}
    document.body.classList.toggle('browsing',Boolean(current));
  }
  render({tabs:[],active:null});window.__TAURI__.event.listen('browser-tabs',event=>render(event.payload)).then(()=>act('state'));
}
