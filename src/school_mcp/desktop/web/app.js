'use strict';
const $ = id => document.getElementById(id);
let token = window.__TAURI__ ? '' : location.hash.slice(1) || sessionStorage.getItem('school-token') || '';
window.addEventListener('native-notice', e => toast(e.detail));
if (token) sessionStorage.setItem('school-token', token);
history.replaceState(null, '', location.pathname);
let activeChat = null, currentChat = null, currentView = 'chat', files = [], snapshot = null;
let taskOffset = 0, taskOffsets = [], nextTaskOffset = null, lastOperation = '', refreshing = false;
let modelRevision=0, modelListRevision=-1;
let diagnosticReport=null, lastUiErrorAt=0;
let campusSites=[],campusProfile=null,campusDirty=false,campusTimetable=null,campusSaving=false,campusTimetableHandled='';
function incidentText(value){return value?.error_id?'（错误编号：'+value.error_id+'）'+(value.diagnostic_saved===false?' 日志未能保存，请检查磁盘和目录权限。':''):'';}
let posting = false, uploading = false, sessionRevision = 0, composeRevision = 0, lastIdentity = '', toastTimer, offlineToast = false;
const ICONS = {
  book: ['M12 6C8 3 4 4 3 5v14c4-2 7-1 9 1 2-2 5-3 9-1V5c-4-1-7-2-9 1Z','M12 6v14'],
  chat: ['M21 11a9 8 0 0 1-9 8H7l-5 3 2-6a8 8 0 0 1-1-5 9 8 0 0 1 18 0Z','M8 10h8M8 14h5'],
  connections: ['M8 8 5 5m11 3 3-3M8 16l-3 3m11-3 3 3','M9 9h6v6H9Z','M2 2h5v5H2ZM17 2h5v5h-5ZM2 17h5v5H2ZM17 17h5v5h-5Z'],
  clipboard: ['M8 5H5v16h14V5h-3','M8 3h8v4H8ZM8 12h8M8 16h5'],
  settings: ['M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1Z','M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0'],
  plus: ['M12 5v14M5 12h14'], history: ['M3 10a9 9 0 1 1 2 8','M3 3v7h7M12 7v6l4 2'],
  shield: ['m12 2 8 3v7c0 5-8 10-8 10S4 17 4 12V5Z','m8 12 3 3 5-6'],
  chevron: ['m9 5 7 7-7 7'], arrow: ['M6 18 18 6M6 6h12v12'], calendar: ['M4 5h16v16H4ZM8 2v6M16 2v6M4 10h16M8 14h2M14 14h2M8 18h2'],
  document: ['M5 2h9l5 5v15H5ZM14 2v6h5M8 12h8M8 16h8'],
  attachment: ['m8 12 7-7a4 4 0 0 1 6 6L10 22a6 6 0 0 1-8-8L13 3','m6 14 8-8a2 2 0 0 1 3 3l-8 8a2 2 0 0 1-3-3'],
  stop: ['M6 6h12v12H6Z'], send: ['m3 3 19 9-19 9 4-9Z','M7 12h15'],
  key: ['M14 7a5 5 0 1 1-10 0 5 5 0 0 1 10 0Z','m12 11 9 9m-4-4 3-3m-6 0 3-3'],
  mail: ['M3 5h18v14H3Z','m3 6 9 7 9-7'], check: ['m5 12 4 4L19 6'],
  refresh: ['M20 8a8 8 0 0 0-14-3L3 8M3 3v5h5','M4 16a8 8 0 0 0 14 3l3-3m0 5v-5h-5'],
  info: ['M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0','M12 11v6M12 7v1'],
  sparkles: ['m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3Z'],
  connection: ['m9 15-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0','m15 9 2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0','m8 16 8-8'],
  exit: ['M10 3H4v18h6M10 12h12m-4-4 4 4-4 4'],
  user: ['M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0','M4 21v-3a8 8 0 0 1 16 0v3'],
  robot: ['M5 7h14v13H5ZM12 3v4M10 3h4M2 12v4m20-4v4M9 11v3m6-3v3M9 17h6'],
  library: ['M4 5h4v15H4ZM10 5h4v15h-4Zm6 0 4-1 3 15-4 1Z'],
  shop: ['M3 10V6l2-3h14l2 3v4M3 10c0 4 6 4 6 0 0 4 6 4 6 0 0 4 6 4 6 0M5 13v8h14v-8M9 21v-6h6v6'],
  close: ['m6 6 12 12M6 18 18 6']
};
const FLOW_LABELS = {mail:'学校邮箱',bb:'Blackboard',academic:'教务系统',timetable:'课程安排',young:'青春科大',finance:'财务平台',library:'图书馆',teach:'教务处通知',icourse:'评课社区',nan7:'南七集市',portal:'一网通办',student:'学工奖学金',spaces:'学习空间',drive:'科大云盘',vlab:'Vlab',web:'公开网页',digest:'信息中心'};
const TASK_STATES = {active:['进行中','info'],running:['处理中','info'],needs_input:['待补材料','attention'],needs_verification:['需核实','warning'],verified:['已核实','good'],waiting:['等待中','info'],stopped:['已停止',''],waiting_for_approval:['待确认','warning'],completed:['已完成','good'],interrupted:['已中断','warning'],failed:['未完成','attention']};
function element(tag, text, cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
function icon(name) { const svg=document.createElementNS('http://www.w3.org/2000/svg','svg'); svg.setAttribute('viewBox','0 0 24 24'); svg.setAttribute('class','icon'); svg.setAttribute('aria-hidden','true'); for(const d of ICONS[name]||ICONS.document){const p=document.createElementNS(svg.namespaceURI,'path');p.setAttribute('d',d);svg.append(p);} return svg; }
for(const n of document.querySelectorAll('[data-icon]')) n.replaceChildren(icon(n.dataset.icon));
function button(text, callback, cls) { const b=element('button',text,cls);b.type='button';b.onclick=callback;return b; }
function badge(text, tone='') { return element('span',text,'status-pill '+tone); }
function previewContent(value, depth=0){
  if(value===null||value===undefined)return element('span','未提供');
  if(typeof value!=='object')return element('span',typeof value==='boolean'?(value?'是':'否'):String(value));
  if(depth>5)return element('pre',JSON.stringify(value,null,2));
  if(Array.isArray(value)){const list=element('ul');if(!value.length)return element('span','无');for(const item of value){const li=element('li');li.append(previewContent(item,depth+1));list.append(li);}return list;}
  const labels={to:'收件人',cc:'抄送',bcc:'密送',subject:'主题',body:'正文',text:'内容',attachments:'附件',files:'材料',title:'标题',name:'名称',comment:'备注',course_name:'课程',assignment_name:'作业',content_sha256:'内容校验值'};
  const fields=element('dl');for(const [key,item] of Object.entries(value)){const dd=element('dd');dd.append(previewContent(item,depth+1));fields.append(element('dt',labels[key]||key),dd);}return fields;
}
function setBadge(id,text,tone='') { const n=$(id);n.textContent=text;n.className='status-pill '+tone; }
function loggedIn(){return Boolean(snapshot?.identity.saved&&!snapshot?.locked);}
function renderNotice(){
  if(!snapshot)return;const s=snapshot,logged=loggedIn();
  const tip=!token&&!window.__TAURI__?'请双击应用重新打开此页面。':currentView==='sites'?'':!s.chrome_installed?'请安装 Google Chrome，然后重新打开应用。':!logged?'先完成学校身份登录，再开始处理校园事务。':currentView==='chat'&&!s.model.tested?'学校身份已就绪，请配置模型 API 并测试连接。':'';
  $('notice').textContent=tip;$('notice').classList.toggle('hidden',!tip);
}
function busy(){return Boolean(posting||uploading||snapshot?.busy);}
function toast(text){clearTimeout(toastTimer);$('toast').textContent=text;$('toast').classList.remove('hidden');toastTimer=setTimeout(()=>$('toast').classList.add('hidden'),6500);}
async function api(path,value){const options={headers:{'X-School-Token':token}};if(value!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(value);}const r=await schoolFetch(path,options);const v=await r.json();if(!r.ok)throw Error((v.error||'请求未完成')+incidentText(v));return v;}
function dateText(value){const d=new Date(value);return Number.isNaN(d.getTime())?'时间未记录':d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});}
function resetChat(){$('artifactFiles')?.replaceChildren();composeRevision++;closeHistory();activeChat=currentChat=null;files=[];$('messages').replaceChildren();$('messages').dataset.signature='';$('welcome').classList.remove('hidden');$('approval').classList.add('hidden');$('progress').classList.add('hidden');$('executePermit').checked=false;$('messageInput').value='';renderFiles();$('conversationScroll').scrollTop=0;}
function closeHistory(){$('historyToggle').setAttribute('aria-expanded','false');document.querySelector('.sidebar').classList.remove('history-open');}
function show(view){closeHistory();const changed=currentView!==view;currentView=view;for(const s of document.querySelectorAll('.view'))s.classList.toggle('hidden',s.id!=='view-'+view);for(const b of document.querySelectorAll('nav button')){b.classList.toggle('selected',b.dataset.view===view);if(b.dataset.view===view)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');}
  $('pageTitle').textContent={chat:activeChat?'校园事务对话':'新的对话',accounts:'账号与连接',tasks:'任务记录',settings:'模型与设置',digest:'信息中心',sites:'校园网站',timetable:'我的课表',profile:'个人资料'}[view];
  renderNotice();
  if(view==='sites')loadSites();if(view==='timetable')loadTimetable();if(view==='profile'&&!campusProfile)loadProfile();
  if(view==='digest')loadDigest();
  if(view==='tasks')loadTasks();if(view==='settings'&&snapshot&&changed){fillModelForm();loadDiagnostics();}
}
function renderControls(){
  const b=busy(),logged=loggedIn();
  for(const id of ['loginIdentity','setupIdentity','saveModel','testModel','fetchModels','quitApp'])$(id).disabled=b;
  for(const id of ['loginMail','setupMail','resumeMail','lockApp','emailVerification','unbindMail','switchAccount','clearAll','fileInput','executePermit','fetchSchoolKeys','connectSchoolModel'])$(id).disabled=b||!logged;
  $('fileInput').closest('label').classList.toggle('disabled',b||!logged);
  $('send').disabled=b||!logged||!snapshot?.model.tested||Boolean(currentChat?.pending);
  $('approve').disabled=$('reject').disabled=b||!logged;
  $('executePermit').disabled=b||!logged||Boolean(currentChat?.pending);
  $('stop').classList.toggle('hidden',!snapshot?.busy);
  for(const n of document.querySelectorAll('#serviceGrid button'))n.disabled=b||!logged;
  for(const id of ['refreshTimetable','saveProfile','reloadProfile'])$(id).disabled=b||!logged||campusSaving;
  for(const n of document.querySelectorAll('#profileFields input'))n.disabled=!logged||campusSaving;
  $('saveProfile').disabled=b||!logged||campusSaving||!campusProfile;
  $('timetableSemester').disabled=$('timetableWeek').disabled=b||!logged;
}
async function refresh(){
  if(refreshing)return;refreshing=true;
  try{
    snapshot=await api('/api/status');offlineToast=false;
    const s=snapshot,logged=loggedIn(),identity=logged?s.identity.account:'';
    if(identity!==lastIdentity){sessionRevision++;lastIdentity=identity;if(!logged)resetChat();resetCampus();digestSnapshot=null;digestDirty=false;digestFormLoaded=false;for(const id of ['digestItems','digestMessages','digestHealth','digestResult','digestSources','digestCourses'])$(id).replaceChildren();$('digestForm').reset();$('digestRequest').value='';digestLastPoll=0;digestSignature='';$('digestNavLabel').textContent='信息中心';}
    $('sideIdentity').textContent=logged?s.identity.account:'尚未登录';$('identityDot').classList.toggle('connected',logged);
    $('modelBadge').textContent=s.model.tested?s.model.model:'模型待配置';$('modelShortcut').classList.toggle('ready',s.model.tested);
    const ready=[logged,s.model.tested];
    $('setupCount').textContent=ready.filter(Boolean).length+' / 2';
    for(const [i,id] of ['setupIdentity','setupModel'].entries()){$(id).classList.toggle('done',ready[i]);$(id).querySelector('.step-status').replaceChildren(icon(ready[i]?'check':'arrow'));}
    $('stepIdentity').textContent=logged?'已登录 · '+s.identity.account:'登录并完成验证';$('stepMail').textContent=s.mail.configured&&logged?'已绑定 · 检查连接':'绑定邮箱';$('stepModel').textContent=s.model.tested?'已通过连接测试':'连接你使用的模型';
    $('onboarding').classList.toggle('hidden',ready.every(Boolean)||Boolean(activeChat));
    $('identityDetail').textContent=logged?'本机身份：'+s.identity.account+' · 登录态已保存':s.locked?'应用已锁定，请重新验证学校登录。':'本人在学校官方窗口完成登录和验证。';
    $('mailDetail').textContent=s.mail.configured&&logged?'邮箱配置已保存；当前连通性请通过下方平台卡片核实。':'登录后自动创建客户端密码，并在本机加密保存。';
    setBadge('identityStatus',logged?'已登录':'待登录',logged?'good':'');setBadge('mailStatus',s.mail.configured&&logged?'已绑定':'待绑定',s.mail.configured&&logged?'good':'');
    $('emailVerification').checked=logged&&s.identity.email_verification;
    $('dataLocation').textContent=s.data_location;$('modelStatus').textContent=s.model.tested?'模型连接与工具调用测试已通过。':s.model.configured?'配置已保存，尚未通过连接测试。':'尚未配置模型。';$('modelStatus').classList.toggle('good',s.model.tested);
    $('startChat').classList.toggle('hidden',!s.model.tested);
    if(s.operation.kind==='model_test'){
      if(s.busy)$('modelStatus').textContent=s.operation.detail||'正在验证模型…';
      else if(s.operation.state==='failed')$('modelStatus').textContent=s.operation.detail;
    }
    if(s.operation.kind==='model_list'&&s.operation.state==='completed'&&modelListRevision===modelRevision){
      const result=s.operation.result||{};$('modelOptions').replaceChildren(...(result.models||[]).map(id=>{const option=element('option');option.value=id;return option;}));
      $('modelListStatus').textContent=result.note||'选择或填写模型名';modelListRevision=-1;
    }
    if(s.operation.kind==='model_list'&&s.operation.state==='failed')$('modelListStatus').textContent=s.operation.detail;
    if(s.operation.kind==='school_model_keys'&&s.operation.state==='completed'&&$('schoolKey').dataset.operation!==s.operation.id){
      const keys=(s.operation.result?.keys||[]).filter(k=>k.status==='active');$('schoolKey').replaceChildren(...keys.map(k=>{const o=element('option',k.name||'学校密钥');o.value=k.id;o.dataset.models=JSON.stringify(k.models||[]);return o;}));$('schoolKey').dataset.operation=s.operation.id;$('schoolModelHint').textContent=s.operation.result?.note||'';schoolKeyChanged();
    }
    if(s.operation.kind==='school_model_import'){$('schoolModelHint').textContent=s.operation.state==='completed'?'学校模型已绑定，工具调用测试通过。':s.operation.detail||'正在绑定…';}
    $('accountOperation').textContent=(s.operation.detail||'')+incidentText(s.operation);$('accountOperation').classList.toggle('hidden',s.operation.state==='idle'||!s.operation.detail);$('accountOperation').classList.toggle('attention',['failed','attention','interrupted'].includes(s.operation.state));
    renderNotice();
    if(!logged){$('taskList').replaceChildren();$('taskCount').textContent='登录后查看记录';$('taskPage').textContent='';$('moreTasks').classList.add('hidden');$('previousTasks').classList.add('hidden');$('chatList').replaceChildren(element('p','登录后，你的对话会留在这里。','side-empty'));delete $('chatList').dataset.signature;}
    renderServices(s.services);renderControls();
    if(logged){await loadChats();if(activeChat)await loadChat(activeChat);}
    renderProgress();
    const key=s.operation.id+':'+s.operation.state;
    if(key!==lastOperation&&['failed','attention'].includes(s.operation.state)){toast(s.operation.detail+incidentText(s.operation));if(currentView==='settings')loadDiagnostics();}
    if(key!==lastOperation&&currentView==='tasks'&&logged)await loadTasks();
    if(key!==lastOperation&&currentView==='digest'&&logged){await loadDigest(false);renderDigestOperation(s.operation);}
    if(logged&&currentView==='profile'&&!campusProfile)await loadProfile();
    if(logged&&s.operation.kind==='campus_timetable'){
      if(s.busy||s.operation.state==='running')$('timetableStatus').textContent='正在从教务系统读取课表…';
      else if(key!==campusTimetableHandled){
        if(s.operation.state==='completed'&&s.operation.result?.saved)await loadTimetable(true);
        else $('timetableStatus').textContent=(s.operation.detail||'课表刷新未完成。')+incidentText(s.operation)+(campusTimetable?.cached?' 当前仍显示上次成功刷新的快照。':'');
        campusTimetableHandled=key;
      }
    }
    if(logged&&Date.now()-digestLastPoll>15000)await loadDigest(false);
    lastOperation=key;
  }catch(e){if(!offlineToast){toast('无法连接本机应用：'+e.message);offlineToast=true;}}
  finally{refreshing=false;}
}
function renderServices(services){
  const signature=JSON.stringify(services)+loggedIn(),grid=$('serviceGrid');if(grid.dataset.signature===signature)return;grid.dataset.signature=signature;grid.replaceChildren();
  const states={unchecked:['尚未检查',''],connected:['已连接','good'],unavailable:['暂不可用','attention'],login_required:['需要登录','warning'],verification_required:['需要本人验证','warning']};
  const icons={mail:'mail',bb:'clipboard',jw:'calendar',young:'sparkles',finance:'document',library:'library',teach:'document',icourse:'book',nan7:'shop'};
  for(const s of services){const card=element('article',undefined,'service'),heading=element('div',undefined,'service-heading');heading.append(icon(icons[s.service]),element('h3',s.label));card.append(heading,badge(...(states[s.state]||['需检查',''])));const actions=element('div',undefined,'actions');
    actions.append(button('检查连接',()=>operation('/api/connect',{service:s.service,reconnect:false})));
    if(['bb','jw','young','finance','library','nan7','portal','student','spaces','drive','vlab'].includes(s.service)){actions.append(button('登录 / 验证',()=>loginService(s.service)));actions.append(button('恢复会话',()=>operation('/api/connect',{service:s.service,reconnect:true})));}
    if(s.service==='icourse')actions.append(button('独立登录',()=>loginService('icourse')));
    if(s.service==='mail')actions.append(button('登录 / 绑定',()=>loginService('mail')));
    card.append(actions);if(s.service!=='teach')card.append(element('p',s.session_saved?'登录信息已保存在本机，使用时自动检查并复用。':s.service==='icourse'?'查看公开课程无需登录；发表评价需评课社区自己的账号。':'首次登录后在本机保存，会话失效时只需重新验证此平台。','service-note'));if(s.next_step)card.append(element('p',s.next_step,'service-note'));else if(s.service==='finance')card.append(element('p','查询与材料预检','service-note'));else if(s.service==='teach')card.append(element('p','公开通知，无需学校账号','service-note'));grid.append(card);
  }
}
async function loadChats(){
  const revision=sessionRevision,v=await api('/api/chats');if(revision!==sessionRevision||!loggedIn())return;
  const list=$('chatList'),signature=JSON.stringify(v.chats)+activeChat;if(list.dataset.signature===signature)return;list.dataset.signature=signature;list.replaceChildren();
  if(!v.chats.length)list.append(element('p','从新的对话开始，记录会自动保留。','side-empty'));
  for(const c of v.chats){const b=button('',()=>{composeRevision++;activeChat=c.id;files=[];renderFiles();$('executePermit').checked=false;show('chat');loadChat(c.id).catch(e=>toast(e.message));loadChats().catch(e=>toast(e.message));},c.id===activeChat?'active':'');const copy=element('span',undefined,'recent-copy');copy.append(element('span',c.title,'recent-title'),element('span',dateText(c.updated_at),'recent-time'));b.append(icon('document'),copy);b.title=c.title;list.append(b);}
}
async function loadChat(id){
  const revision=sessionRevision,chat=await api('/api/chat?id='+encodeURIComponent(id));if(activeChat!==id||revision!==sessionRevision||!loggedIn())return;currentChat=chat;
  const list=$('messages'),scroll=$('conversationScroll'),nearBottom=scroll.scrollHeight-scroll.clientHeight-scroll.scrollTop<100,signature=JSON.stringify([chat.messages,chat.artifacts]);
  if(list.dataset.signature!==signature){list.replaceChildren();for(const m of chat.messages){const row=element('div',undefined,'message-row '+(m.role==='user'?'user':'agent')),avatar=element('span',undefined,'message-avatar'),stack=element('div',undefined,'message-stack');avatar.append(icon(m.role==='user'?'user':'robot'));stack.append(element('div',m.role==='user'?'你':'学校助手','message-author'),element('div',m.content,'message '+m.role));row.append(avatar,stack);list.append(row);}list.dataset.signature=signature;if(nearBottom)scroll.scrollTop=scroll.scrollHeight;}
  let outputs=$('artifactFiles');if(!outputs){outputs=element('div',undefined,'artifact-files');outputs.id='artifactFiles';list.after(outputs);}outputs.replaceChildren();
  for(const artifact of chat.artifacts||[]){outputs.append(button('下载 '+artifact.name+' · '+Math.ceil(artifact.size_bytes/1024)+' KB',async()=>{
    try{const r=await schoolFetch('/api/artifact?chat='+encodeURIComponent(id)+'&id='+encodeURIComponent(artifact.id),{headers:{'X-School-Token':token}});if(!r.ok)throw Error((await r.json()).error);const url=URL.createObjectURL(await r.blob()),a=element('a');a.href=url;a.download=artifact.name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){toast(e.message);}
  },'secondary'));}
  $('welcome').classList.add('hidden');$('onboarding').classList.add('hidden');$('approval').classList.toggle('hidden',!chat.pending);
  if(chat.pending){const signature=JSON.stringify(chat.pending);if($('approvalPreview').dataset.signature!==signature){$('approvalPreview').replaceChildren(previewContent(chat.pending.preview));$('approvalPreview').dataset.signature=signature;$('approvalArgs').textContent=JSON.stringify(chat.pending.arguments,null,2);}}
  renderProgress();renderControls();
}
function renderProgress(){
  const box=$('progress'),chat=currentChat,operation=snapshot?.operation;if(!chat||chat.pending){box.classList.add('hidden');return;}
  const running=chat.state==='running'&&snapshot?.busy;
  const interrupted=chat.state==='interrupted';
  if(!running&&!interrupted){box.classList.add('hidden');return;}
  const last=chat.events.at(-1),signature=JSON.stringify([running,last]);
  box.classList.remove('hidden');if(box.dataset.signature===signature)return;box.dataset.signature=signature;box.classList.toggle('running',running);box.classList.toggle('attention',interrupted);box.replaceChildren();
  const heading=element('div',undefined,'progress-heading');heading.append(icon(running?'refresh':'info'),element('span',running?'正在处理':'已停下，进度已保存'));box.append(heading,element('p',last?.text||operation?.detail||'请查看原任务记录。'));
  if(interrupted&&!busy()){
    const problem=chat.last_failure||{}, code=problem.error_code||chat.last_error_code||'', actions=element('div',undefined,'actions');
    if(code.startsWith('model_')||['invalid_model_response','context_capacity'].includes(code))actions.append(button('检查模型设置',()=>show('settings')));
    else if(problem.service==='mail')actions.append(button('绑定或检查邮箱',()=>{show('accounts');$('loginMail').focus();}));
    else if(problem.service==='icourse')actions.append(button('登录评课社区',()=>loginService('icourse')));
    else if(['authentication_required','identity_required','manual_verification_required'].includes(code))actions.append(button('登录 / 完成验证',()=>loginService(['bb','jw','young','finance','library','nan7','portal','student','spaces','drive','vlab'].includes(problem.service)?problem.service:'identity')));
    else actions.append(button('检查平台连接',()=>show('accounts')));
    actions.append(button('继续原任务',()=>{$('messageInput').value='请先核实原任务状态，再继续处理。';$('messageInput').focus();}));box.append(actions);
  }
  if(running){const previous=chat.events.filter(e=>e.kind==='result').at(-1);if(previous)box.append(element('div','已收到工具回执，继续核对下一步。','progress-step'));}
}
async function operation(path,value){
  if(posting)return false;posting=true;renderControls();
  try{await api(path,value);await refresh();return true;}catch(e){toast(e.message);return false;}finally{posting=false;renderControls();}
}
function confirmAction(title,text,danger=false){
  const dialog=$('confirmDialog');if(dialog.open)return Promise.resolve(false);
  $('confirmTitle').textContent=title;$('confirmText').textContent=text;$('confirmAction').className=danger?'danger':'primary';dialog.returnValue='';dialog.showModal();
  return new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'),{once:true}));
}
async function loginService(service){if(service==='mail'&&!snapshot?.mail.configured&&!(await confirmAction('绑定学校邮箱','将打开学校邮箱。你登录后，应用会创建一个客户端专用密码，并在本机加密保存。')))return;await operation('/api/login',{service});}
function emptyTasks(title,text){const n=element('div',undefined,'empty-state');n.append(icon('clipboard'),element('h3',title),element('p',text));$('taskList').replaceChildren(n);}
async function loadTasks(){
  if(!loggedIn()){emptyTasks('登录后查看任务','在“账号与连接”完成学校身份登录。');return;}
  const revision=sessionRevision,offset=taskOffset;
  try{const v=await api('/api/tasks?offset='+offset);if(revision!==sessionRevision||offset!==taskOffset||!loggedIn())return;
    $('taskCount').textContent='本页 '+v.tasks.length+' 项';$('taskPage').textContent='第 '+(taskOffsets.length+1)+' 页';nextTaskOffset=v.next_offset;
    $('moreTasks').classList.toggle('hidden',v.next_offset===null);$('previousTasks').classList.toggle('hidden',!taskOffsets.length);
    const list=$('taskList');list.replaceChildren();if(!v.tasks.length){emptyTasks('这里还没有任务',v.next_offset!==null?'当前扫描页无可显示的记录，可以继续翻页。':'在对话中开始办理事务后，进度会保存在这里。');return;}
    const table=element('table',undefined,'task-table'),head=element('thead'),hr=element('tr'),body=element('tbody');
    for(const label of ['任务名称','所属平台','更新时间','状态','操作']){const th=element('th',label);th.scope='col';hr.append(th);}head.append(hr);table.append(head,body);
    for(const t of v.tasks){const tr=element('tr',undefined,'task-row');tr.dataset.state=t.state;
      const title=element('td'),platform=element('td',FLOW_LABELS[t.workflow]||'校园事务','task-platform'),date=element('td',dateText(t.updated_at||t.created_at),'task-date'),status=element('td'),actions=element('td');
      title.append(element('span',t.label,'task-title'));status.append(badge(...(TASK_STATES[t.state]||['需查看',''])));
      const row=element('tr',undefined,'task-detail hidden'),detail=element('td');detail.colSpan=5;detail.append(element('p',t.write_blocked?'结果需要核实，已拦住重复写入。':'以下是查询原业务回执的入口；任务状态不代替业务结果。'),element('pre',t.links.length?JSON.stringify(t.links,null,2):'当前没有原业务回执入口，可打开关联对话查看准备记录。'));row.append(detail);
      const controls=element('div',undefined,'task-actions');controls.append(button('打开对话',()=>resumeTask(t.task_id)));const disclosure=button('查看回执',()=>{row.classList.toggle('hidden');disclosure.setAttribute('aria-expanded',String(!row.classList.contains('hidden')));});disclosure.setAttribute('aria-expanded','false');controls.append(disclosure);actions.append(controls);tr.append(title,platform,date,status,actions);body.append(tr,row);
    }list.append(table);
  }catch(e){toast(e.message);}
}
async function resumeTask(id){
  const revision=sessionRevision;
  try{const chats=await api('/api/chats');for(const c of chats.chats){if(revision!==sessionRevision)return;const detail=await api('/api/chat?id='+encodeURIComponent(c.id));if(revision!==sessionRevision)return;if(detail.tasks.includes(id)){composeRevision++;activeChat=c.id;files=[];renderFiles();$('executePermit').checked=false;show('chat');await loadChat(c.id);$('messageInput').value='请先核实原任务状态，再继续处理。';return;}}toast('最近对话中没有找到关联项，请根据回执入口核实原任务。');}catch(e){toast(e.message);}
}
function renderFiles(){
  const list=$('fileChips');list.replaceChildren();
  for(const [i,file] of files.entries()){const chip=element('span',undefined,'file-chip'),remove=button('',()=>{files.splice(i,1);renderFiles();});remove.append(icon('close'));remove.setAttribute('aria-label','移除材料 '+file.name);chip.append(icon('document'),element('span',file.name),remove);list.append(chip);}
}
for(const b of document.querySelectorAll('nav button'))b.onclick=()=>show(b.dataset.view);
for(const b of document.querySelectorAll('[data-prompt]'))b.onclick=()=>{$('messageInput').value=b.dataset.prompt;$('messageInput').focus();};
$('newChat').onclick=()=>{resetChat();show('chat');if(snapshot)$('onboarding').classList.toggle('hidden',loggedIn()&&snapshot.model.tested);if(loggedIn())loadChats().catch(e=>toast(e.message));renderControls();};
$('modelShortcut').onclick=()=>show('settings');
$('historyToggle').onclick=()=>{const open=document.querySelector('.sidebar').classList.toggle('history-open');$('historyToggle').setAttribute('aria-expanded',String(open));};
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeHistory();});
$('setupIdentity').onclick=$('loginIdentity').onclick=()=>loginService('identity');
$('setupMail').onclick=$('loginMail').onclick=()=>loginService('mail');
$('resumeMail').onclick=()=>loginService('mail_resume');$('setupModel').onclick=()=>show('settings');
$('emailVerification').onchange=async()=>{const ok=await operation('/api/email-verification',{enabled:$('emailVerification').checked});if(!ok)$('emailVerification').checked=Boolean(snapshot?.identity.email_verification);};
$('composer').onsubmit=async e=>{
  e.preventDefault();const text=$('messageInput').value.trim();if(!text||busy()||!loggedIn())return;
  posting=true;renderControls();
  try{const v=await api('/api/chat/send',{chat_id:activeChat,text,files:files.map(f=>f.path),execute_authorized:$('executePermit').checked});activeChat=v.chat_id;$('messageInput').value='';$('executePermit').checked=false;files=[];renderFiles();await refresh();$('conversationScroll').scrollTop=$('conversationScroll').scrollHeight;show('chat');}catch(err){toast(err.message);}finally{posting=false;renderControls();}
};
$('messageInput').onkeydown=e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();$('composer').requestSubmit();}};
$('fileInput').onchange=async e=>{
  if(uploading)return;const selected=[...e.target.files],revision=sessionRevision,composition=composeRevision;uploading=true;renderControls();
  try{for(const file of selected){if(revision!==sessionRevision||composition!==composeRevision)break;try{
    if(files.length>=10)throw Error('每次最多添加 10 个材料文件');if(!file.size||file.size>50*1024*1024)throw Error('单个文件须在 50 MiB 以内，且不能为空');
    const r=await schoolFetch('/api/upload',{method:'POST',headers:{'X-School-Token':token,'X-File-Name':encodeURIComponent(file.name),'Content-Type':'application/octet-stream'},body:file});const v=await r.json();if(!r.ok)throw Error(v.error);if(revision===sessionRevision&&composition===composeRevision){files.push(v);renderFiles();}
  }catch(err){toast(err.message);}}}finally{e.target.value='';uploading=false;renderControls();}
};
function modelValues(){return {base_url:$('apiBase').value.trim(),model:$('apiModel').value.trim(),api_key:$('apiKey').value};}
function fillModelForm(){
  const config=snapshot?.model||{}, preset=snapshot?.model_providers?.[0]||{base_url:'https://api.deepseek.com',model:'deepseek-flash'};
  $('apiProvider').value=(snapshot?.model_providers||[]).find(p=>p.base_url===config.base_url)?.id||(!config.base_url?'deepseek':'custom');
  $('schoolModelSetup').classList.toggle('hidden',$('apiProvider').value!=='ustc');
  $('apiBase').value=config.base_url||preset.base_url;$('apiModel').value=config.model||preset.model;$('apiKey').value='';modelRevision++;
}
$('modelForm').addEventListener('input',()=>modelRevision++);
$('apiProvider').onchange=()=>{const p=snapshot.model_providers.find(v=>v.id===$('apiProvider').value);$('schoolModelSetup').classList.toggle('hidden',$('apiProvider').value!=='ustc');if(p){$('apiBase').value=p.base_url;$('apiModel').value=p.model;$('apiKey').value='';$('modelOptions').replaceChildren();modelRevision++;}};
$('apiBase').addEventListener('input',()=>{const base=$('apiBase').value.trim().replace(/\/$/,'');$('apiProvider').value=(snapshot?.model_providers||[]).find(p=>p.base_url.replace(/\/$/,'')===base)?.id||'custom';$('schoolModelSetup').classList.toggle('hidden',$('apiProvider').value!=='ustc');});
$('modelForm').onsubmit=async e=>{e.preventDefault();if(busy())return;const ok=await operation('/api/model/connect',modelValues());if(ok){$('apiKey').value='';$('modelStatus').textContent='配置已加密保存，正在测试…';}};
$('fetchModels').onclick=async()=>{if(busy())return;modelListRevision=modelRevision;$('modelListStatus').textContent='正在获取模型列表…';await operation('/api/model/models',modelValues());};
function schoolKeyChanged(){const models=JSON.parse($('schoolKey').selectedOptions[0]?.dataset.models||'[]');$('modelOptions').replaceChildren(...models.map(id=>{const o=element('option');o.value=id;return o;}));if(!models.includes($('apiModel').value))$('apiModel').value=models.find(x=>x==='deepseek-flash')||models[0]||'';}
$('schoolKey').onchange=schoolKeyChanged;
$('fetchSchoolKeys').onclick=()=>operation('/api/model/school-keys',{});
$('connectSchoolModel').onclick=()=>operation('/api/model/school-connect',{key_id:$('schoolKey').value,model:$('apiModel').value.trim()});
$('startChat').onclick=()=>show('chat');
$('testModel').onclick=()=>operation('/api/model/test',{});$('stop').onclick=()=>operation('/api/stop',{});
for(const [id,approve] of [['approve',true],['reject',false]])$(id).onclick=()=>{if(currentChat?.pending){$('executePermit').checked=false;operation('/api/chat/approve',{chat_id:activeChat,approval_id:currentChat.pending.id,approve});}};
async function clear(scope,text){if(!(await confirmAction(scope==='logout'?'退出学校登录':'确认账号管理操作',text,scope!=='logout')))return;const ok=await operation('/api/clear',{scope,confirmation:scope});if(ok&&['account','all','logout'].includes(scope)){resetChat();taskOffset=0;taskOffsets=[];show('accounts');}}
$('lockApp').onclick=()=>clear('logout','退出后需重新验证学校登录才能查看记录。是否退出？');
$('unbindMail').onclick=()=>clear('mail','解除本机邮箱绑定并关闭验证码读取？网站上的客户端密码不会自动撤销。');
$('switchAccount').onclick=()=>clear('account','清除当前学校账号的本机登录、聊天、附件及任务记录，保留模型配置，然后登录另一个账号？');
$('clearAll').onclick=()=>clear('all','删除本应用保存的学校账号数据、聊天、附件、任务和模型 API 配置？这无法撤销。');
$('quitApp').onclick=async()=>{try{await api('/api/shutdown',{});clearInterval(pollTimer);$('notice').textContent='应用已退出，可以关闭此页面。';$('notice').classList.remove('hidden');for(const n of document.querySelectorAll('button,input,textarea'))n.disabled=true;toast('应用已退出。');}catch(e){toast(e.message);}};
$('refreshTasks').onclick=()=>{taskOffset=0;taskOffsets=[];loadTasks();};
$('moreTasks').onclick=()=>{if(nextTaskOffset!==null){taskOffsets.push(taskOffset);taskOffset=nextTaskOffset;loadTasks();}};
$('previousTasks').onclick=()=>{if(taskOffsets.length){taskOffset=taskOffsets.pop();loadTasks();}};
async function loadDiagnostics(){
  diagnosticReport=null;$('copyDiagnostics').disabled=$('exportDiagnostics').disabled=true;
  try{
    const report=await api('/api/diagnostics');
    $('diagnosticsEvents').replaceChildren();$('diagnosticsPreview').classList.toggle('hidden',!report.available);
    if(!report.available){$('diagnosticsJson').textContent='';$('diagnosticsStatus').textContent=report.note;return;}
    diagnosticReport=report;
    $('diagnosticsStatus').textContent=report.events.length?'共 '+report.events.length+' 组错误，累计 '+report.events.reduce((n,e)=>n+e.count,0)+' 次。下方显示最近 20 组，完整记录可展开预览。':'当前没有诊断错误记录。';
    for(const e of report.events.slice(0,20)){
      const item=element('div',undefined,'diagnostic-event');
      item.append(element('strong',e.error_id+' · '+(FLOW_LABELS[e.service]||({app:'应用',model:'模型',agent:'智能体'}[e.service]))),
        element('p',e.code+' · '+e.stage+' · '+dateText(e.last_at*1000)+' · '+e.count+' 次'),element('p',e.outcome_note));
      $('diagnosticsEvents').append(item);
    }
    $('diagnosticsJson').textContent=JSON.stringify(report,null,2);
    $('copyDiagnostics').disabled=$('exportDiagnostics').disabled=false;
  }catch(e){$('diagnosticsStatus').textContent='无法读取诊断日志：'+e.message;}
}
$('refreshDiagnostics').onclick=loadDiagnostics;
$('copyDiagnostics').onclick=async()=>{
  if(!diagnosticReport)return;
  const text=diagnosticReport.events.slice(0,20).map(e=>[e.error_id,e.service,e.stage,e.code,'累计 '+e.count+' 次',e.outcome_note].join(' · ')).join('\n')||'当前没有诊断错误记录。';
  try{await navigator.clipboard.writeText(text);toast('已复制脱敏错误摘要。');}catch(e){$('diagnosticsPreview').open=true;toast('剪贴板不可用，请从报告预览中手动复制。');}
};
$('exportDiagnostics').onclick=()=>{
  if(!diagnosticReport)return;
  const url=URL.createObjectURL(new Blob([JSON.stringify(diagnosticReport,null,2)],{type:'application/json;charset=utf-8'}));
  const link=element('a');link.href=url;link.download='school-diagnostics-'+new Date().toISOString().slice(0,10)+'.json';
  document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('已导出当前预览的报告，请自行选择是否分享。');
};
$('clearDiagnostics').onclick=async()=>{
  if(!await confirmAction('清理诊断日志','只清理错误诊断记录；学校登录、聊天、任务回执和附件都会保留。',true))return;
  try{await api('/api/diagnostics/clear',{confirmation:'diagnostics'});await loadDiagnostics();toast('诊断日志已清理。');}catch(e){toast(e.message);}
};
function reportUiError(){
  if(!token||Date.now()-lastUiErrorAt<30000)return;lastUiErrorAt=Date.now();
  // No browser error message, stack, URL or form data leaves this handler.
  api('/api/diagnostics/ui-error',{}).then(v=>toast('界面出现异常，请刷新后查看安全日志。'+incidentText(v))).catch(()=>{});
}
window.addEventListener('error',reportUiError);window.addEventListener('unhandledrejection',reportUiError);
let digestSnapshot=null,digestDirty=false,digestFormLoaded=false,digestLastPoll=0,digestSignature='';
function csvValues(id){return $(id).value.split(/[,，\n]/).map(s=>s.trim()).filter(Boolean);}
function digestSettings(enabled){return {...(digestSnapshot?.settings||{}),enabled,
  sources:[...document.querySelectorAll('#digestSources input:checked')].map(x=>x.value),
  keywords:csvValues('digestKeywords'),exclude:csvValues('digestExclude'),locations:csvValues('digestLocations'),
  interval_minutes:Number($('digestInterval').value),daily_time:$('digestDaily').value,urgent_hours:Number($('digestUrgent').value),
  max_pages:Number($('digestPages').value),detail_limit:Number($('digestDetails').value),
  email:$('digestEmail').value.trim(),email_enabled:$('digestEmailEnabled').checked,
  bb_courses:[...document.querySelectorAll('#digestCourses input:checked')].map(x=>x.value)};}
function safeSourceLink(url){try{const u=new URL(url);if(!['http:','https:'].includes(u.protocol)||u.username||u.password)return null;const a=element('a','查看原文');a.href=u.href;a.target='_blank';a.rel='noreferrer noopener';return a;}catch{return null;}}
async function loadDigest(fill=true){
  if(!loggedIn()){$('digestStatus').textContent='请先登录学校身份。';$('digestItems').replaceChildren();$('digestMessages').replaceChildren();return;}
  const revision=sessionRevision;digestLastPoll=Date.now();
  try{const v=await api('/api/digest');if(revision!==sessionRevision||!loggedIn())return;digestSnapshot=v;
    const unread=v.messages.filter(x=>!x.read).length;$('digestNavLabel').textContent='信息中心'+(unread?' · '+unread:'');
    const signature=JSON.stringify([v.items,v.messages,v.source_health]);
    const selected=new Set([...document.querySelectorAll('#digestItems input:checked')].map(x=>x.value));
    $('digestStatus').textContent=(v.settings.enabled?(v.worker_running?'提醒已开启，后台正在运行':'提醒已开启，后台未运行，请重新启用'):'当前仅预览，推送已暂停')+' · '+v.total_matched+' 条匹配事项'+(v.truncated?'（仅展示前 100 条，缩小范围可查看）':'');
    if(!digestFormLoaded||(fill&&!digestDirty)){
      const s=v.settings;$('digestSources').replaceChildren();for(const source of v.sources){const label=element('label',undefined,'digest-check'),input=document.createElement('input');input.type='checkbox';input.value=source.id;input.checked=s.sources.includes(source.id);label.append(input,document.createTextNode(source.label));$('digestSources').append(label);}
      for(const [id,key] of [['digestKeywords','keywords'],['digestExclude','exclude'],['digestLocations','locations']])$(id).value=s[key].join(', ');
      for(const [id,key] of [['digestInterval','interval_minutes'],['digestDaily','daily_time'],['digestUrgent','urgent_hours'],['digestPages','max_pages'],['digestDetails','detail_limit'],['digestEmail','email']])$(id).value=s[key];
      $('digestEmailEnabled').checked=s.email_enabled;renderDigestCourses(s.bb_courses.map(id=>({id,name:'已选课程 '+id})));digestFormLoaded=true;
    }
    if(signature===digestSignature)return;digestSignature=signature;
    $('digestHealth').replaceChildren();const names=Object.fromEntries(v.sources.map(s=>[s.id,s.label]));
    for(const id of v.settings.sources){const x=v.source_health[id];$('digestHealth').append(element('p',names[id]+'：'+(!x?'尚未采集':x.state==='failed'?'读取失败，已有内容可能过时；错误编号 '+x.error_id:(x.complete?'已读取当前范围':'仅扫描部分列表')+(x.detail_failures?'；部分正文读取失败':''))+(x?.last_success?' · 最近成功 '+dateText(x.last_success):'')+(x?.scope?' · '+x.scope:'')+(x?.unsupported_links?' · '+x.unsupported_links+' 个链接尚未适配':'')+(x?.excluded_low_quality?' · 已排除 '+x.excluded_low_quality+' 条信息不足的内容':'')+(x?.next_action?' · '+x.next_action:'')));}
    $('digestItems').replaceChildren();for(const x of v.items){const card=element('article',undefined,'card digest-item'),label=element('label',undefined,'digest-check'),check=document.createElement('input');check.type='checkbox';check.value=x.id;check.checked=selected.has(x.id);check.disabled=!(x.start||x.deadline);label.append(check,element('b',x.title));card.append(label,element('p',(names[x.source]||x.source)+(x.company?' · '+x.company:'')+' · '+(x.deadline?'截止 '+x.deadline:x.start?'时间 '+x.start:'时间待确认')));
      if(x.time_warning)card.append(element('p',x.time_warning,'attention'));if(x.time_candidates){for(const [field,label] of [['start','活动时间候选'],['end','结束时间候选'],['deadline','截止时间候选']])if(x.time_candidates[field])card.append(element('p',label+'：'+x.time_candidates[field]+'（海报识别，尚未核对）'));}if(x.poster_ocr?.skipped)card.append(element('p','有 '+x.poster_ocr.skipped+' 张图片未识别，请查看原文。'));if(x.location)card.append(element('p',x.location));if(x.summary){const details=element('details'),summary=element('summary',x.summary_truncated?'查看摘要（已截取，全文见原文）':'查看摘要');details.append(summary,element('p',x.summary));card.append(details);}card.append(element('small','匹配：'+(x.matched_by||[]).join('、')));const a=safeSourceLink(x.source_url);if(a)card.append(a);$('digestItems').append(card);}
    if(!v.items.length)$('digestItems').append(element('p','尚无匹配事项；请检查来源状态，或点击“采集并预览”。'));
    $('digestMessages').replaceChildren();for(const x of v.messages){const card=element('article',undefined,'card'),details=element('details');details.append(element('summary',dateText(x.created_at)+(x.read?' · 已读':' · 未读')),element('pre',x.text));card.append(details,element('p','接收状态：'+({app_only:'已保存到应用内',accepted:'服务器已接受',sending:'发送中或结果待核实',unknown:'结果待核实，不会自动重发',prepared:'已准备，尚未发送',blocked_sender_changed:'发件账号变化，已停止'}[x.delivery.status]||x.delivery.status)));
      if(!x.read)card.append(button('标记已读',async()=>{try{await api('/api/digest/read',{message_id:x.id});await loadDigest(false);}catch(e){toast(e.message);}}));$('digestMessages').append(card);}
  }catch(e){if(revision===sessionRevision&&loggedIn())$('digestStatus').textContent=e.message;}
}
function renderDigestCourses(courses){const chosen=new Set([...document.querySelectorAll('#digestCourses input:checked')].map(x=>x.value).concat(digestSnapshot?.settings.bb_courses||[]));$('digestCourses').replaceChildren();for(const x of courses){const id=x.id||x.course_id;if(!id)continue;const label=element('label',undefined,'digest-check'),input=document.createElement('input');input.type='checkbox';input.value=id;input.checked=chosen.has(id);label.append(input,document.createTextNode(x.name||x.title||id));$('digestCourses').append(label);}}
function renderDigestOperation(op){if(!op?.kind?.startsWith('digest_'))return;const r=op.result||{};const box=$('digestResult');box.replaceChildren();if(op.state==='running'){box.textContent='正在处理，请稍候…';return;}if(op.state==='failed'){box.textContent=op.detail+incidentText(op);return;}if(op.kind==='digest_plan'&&r.settings){box.append(previewContent(r.preview),element('p',r.note));for(const message of r.missing||[])box.append(element('p',message,'attention'));box.append(button('应用到表单（尚不保存）',()=>applyDigestPlan(r.settings)));}if(op.kind==='digest_preview_report'&&r.message_id)box.append(element('p','试阅简报已放入下方“收到的简报”，仅应用内接收。'));if(op.kind==='digest_courses'&&r.courses)renderDigestCourses(r.courses);if(op.kind==='digest_summary'&&r.text)box.append(element('pre',r.text),element('p',r.note));if(op.kind==='digest_calendar'&&r.chat_id){box.append(element('p','已生成 '+r.count+' 个日程，跳过 '+r.skipped.length+' 项时间未明确的安排。'),button('打开日历下载',async()=>{activeChat=r.chat_id;show('chat');await loadChat(r.chat_id);await loadChats();}));if(r.skipped.length)box.append(element('pre',JSON.stringify(r.skipped,null,2)));}}
async function saveDigest(enabled){if(enabled&&!await confirmAction('开启校园信息提醒','按当前来源和筛选条件定时采集，北京时间 '+$('digestDaily').value+' 汇总新消息；临近截止另行提醒。'+($('digestEmailEnabled').checked?'简报会发送到 '+$('digestEmail').value+'。':'简报保存在应用信息中心。')))return;
  try{const r=await api('/api/digest/configure',{settings:digestSettings(enabled),authorized:enabled});digestDirty=false;await loadDigest();toast(enabled?(r.worker?.running?'提醒已开启。':'已保存，请查看后台状态。'):'已保存，推送暂停。');}catch(e){toast(e.message);}}
function applyDigestPlan(s){
  for(const input of document.querySelectorAll('#digestSources input'))input.checked=s.sources.includes(input.value);
  for(const [id,key] of [['digestKeywords','keywords'],['digestExclude','exclude'],['digestLocations','locations']])$(id).value=s[key].join(', ');
  for(const [id,key] of [['digestInterval','interval_minutes'],['digestDaily','daily_time'],['digestUrgent','urgent_hours'],['digestPages','max_pages'],['digestDetails','detail_limit'],['digestEmail','email']])$(id).value=s[key];
  $('digestEmailEnabled').checked=s.email_enabled;
  for(const input of document.querySelectorAll('#digestCourses input'))input.checked=s.bb_courses.includes(input.value);
  digestDirty=true;$('digestOptions').open=true;toast('已填入表单；请核对后保存或开启。');
}
$('digestPresetPlan').onclick=()=>operation('/api/digest/plan',{preset:$('digestPreset').value});
$('digestRequestPlan').onclick=()=>operation('/api/digest/plan',{request:$('digestRequest').value});
$('digestPreviewReport').onclick=()=>operation('/api/digest/preview-report',{});
$('digestForm').oninput=()=>{digestDirty=true;};$('digestForm').onsubmit=e=>{e.preventDefault();saveDigest(false);};
$('digestEnable').onclick=()=>saveDigest(true);$('digestReload').onclick=()=>loadDigest(false);
$('digestRefresh').onclick=async()=>{if(digestDirty){toast('先保存来源与筛选条件，再采集。');return;}await operation('/api/digest/refresh',{});};
$('digestLoadCourses').onclick=()=>operation('/api/digest/courses',{});
$('digestSummary').onclick=()=>operation('/api/digest/summarize',{});
$('digestExport').onclick=()=>operation('/api/digest/calendar',{item_ids:[...document.querySelectorAll('#digestItems input:checked')].map(x=>x.value)});
$('digestTimetable').onclick=()=>operation('/api/digest/calendar',{semester_id:0});
function resetCampus(){
  campusProfile=null;campusDirty=false;campusTimetable=null;campusTimetableHandled='';
  $('profileFields').replaceChildren();$('profileAccount').textContent='登录后查看';$('profileStatus').textContent='尚未读取。';
  $('timetableBoard').replaceChildren();$('timetableUnplaced').replaceChildren();$('timetableStatus').textContent='登录学校身份后查看。';
  const current=element('option','当前学期');current.value='0';$('timetableSemester').replaceChildren(current);
  delete $('timetableSemester').dataset.loaded;
  const all=element('option','全部周次');all.value='0';$('timetableWeek').replaceChildren(all);
}
async function loadSites(){try{if(!campusSites.length)campusSites=(await api('/api/campus/sites')).sites;renderSites();}catch(e){toast(e.message);}}
function renderSites(){
  const term=$('siteSearch').value.trim().toLowerCase(),grid=$('siteGrid');grid.replaceChildren();
  for(const site of campusSites.filter(s=>(s.name+s.description+s.category).toLowerCase().includes(term))){
    const card=element('article',undefined,'site-card'),link=element('a','打开网站');link.href=site.url;link.target='_blank';link.rel='noopener noreferrer';
    card.append(element('small',site.category,'muted'),element('h3',site.name),element('p',site.description),link);grid.append(card);
  }
  if(!grid.children.length)grid.append(element('p','没有匹配的网站。','muted'));
}
async function loadProfile(force=false){
  if(!loggedIn()||campusSaving||campusProfile&&!force)return;
  const revision=sessionRevision;
  try{
    const value=await api('/api/campus/profile');if(revision!==sessionRevision||!loggedIn())return;
    campusProfile=value;campusDirty=false;$('profileAccount').textContent=value.account;$('profileFields').replaceChildren();
    for(const [key,field] of Object.entries(value.fields)){
      const label=element('label',field.label),input=element('input');input.id='profile-'+key;input.name=key;input.maxLength=field.max_length;input.value=value.values[key]||'';
      input.type=key==='email'?'email':key==='entry_month'?'month':'text';input.autocomplete='off';if(key==='phone')input.inputMode='tel';
      label.append(input);$('profileFields').append(label);
    }
    $('profileStatus').textContent=value.updated_at?'上次保存：'+dateText(value.updated_at)+' · 本人填写，未经学校核验。':'还没有保存资料。';renderControls();
  }catch(e){$('profileStatus').textContent=e.message;}
}
async function saveCampusProfile(event){
  event.preventDefault();if(!campusProfile||campusSaving||!loggedIn())return;
  const revision=sessionRevision,values=Object.fromEntries([...document.querySelectorAll('#profileFields input')].map(e=>[e.name,e.value]));
  campusSaving=true;renderControls();
  try{const result=await api('/api/campus/profile',{values,revision:campusProfile.revision});if(revision!==sessionRevision||!loggedIn())return;campusProfile=result;campusDirty=false;$('profileStatus').textContent='已保存到本机 · '+dateText(result.updated_at);}
  catch(e){if(revision===sessionRevision)$('profileStatus').textContent=e.message;}
  finally{campusSaving=false;renderControls();}
}
async function loadTimetable(force=false){
  if(!loggedIn())return;const revision=sessionRevision;
  try{
    if(!campusTimetable||force){const result=await api('/api/campus/timetable');if(revision!==sessionRevision||!loggedIn())return;campusTimetable=result;}
    if(!campusTimetable.cached){$('timetableStatus').textContent='还没有课表快照，请点击“刷新课表”。';return;}
    const {data,semesters}=campusTimetable;
    if(force||!$('timetableSemester').dataset.loaded){
      $('timetableSemester').replaceChildren(...semesters.map(s=>{const o=element('option',s.name);o.value=s.id;return o;}));$('timetableSemester').value=data.semester.id;$('timetableSemester').dataset.loaded='1';
      const weeks=[...new Set((data.activities||[]).flatMap(a=>Array.isArray(a.weeks)?a.weeks:[]).filter(w=>Number.isInteger(w)&&w>0))].sort((a,b)=>a-b);
      if(Number.isInteger(data.current_week)&&data.current_week>0&&!weeks.includes(data.current_week))weeks.push(data.current_week);weeks.sort((a,b)=>a-b);
      $('timetableWeek').replaceChildren(...[0,...weeks].map(w=>{const o=element('option',w?'第 '+w+' 周':'全部周次');o.value=w;return o;}));$('timetableWeek').value=weeks.includes(data.current_week)?data.current_week:0;
    }
    renderTimetable();
  }catch(e){$('timetableStatus').textContent=e.message;}
}
function campusText(value){if(value===null||value===undefined)return '';if(Array.isArray(value))return value.map(campusText).filter(Boolean).join('、');if(typeof value==='object')return String(value.nameZh||value.name||value.text||'');return String(value);}
function renderTimetable(){
  const board=$('timetableBoard'),unplaced=$('timetableUnplaced');board.replaceChildren();unplaced.replaceChildren();
  if(!campusTimetable?.cached)return;
  const {data,updated_at}=campusTimetable,week=Number($('timetableWeek').value);
  if(Number($('timetableSemester').value)!==data.semester.id){$('timetableStatus').textContent='已切换学期，点击“刷新课表”读取所选学期。';return;}
  $('timetableStatus').textContent=data.semester.name+' · '+(week?'第 '+week+' 周':'全部周次')+' · 快照更新于 '+dateText(updated_at);
  const activities=data.activities||[],rows=activities.filter(a=>!week||!Array.isArray(a.weeks)||!a.weeks.length||a.weeks.includes(week));
  function lesson(a){
    const card=element('article',undefined,'lesson-card');card.append(element('h3',campusText(a.course_name)||'课程名称未提供'));
    if(a.start_period!=null)card.append(element('p','第 '+a.start_period+(a.end_period!=null?'–'+a.end_period:'')+' 节','lesson-time'));
    if(/^\d{2}:\d{2}(:\d{2})?$/.test(a.start||''))card.append(element('p',a.start+(a.end?'–'+a.end:'')));
    card.append(element('p',[a.campus,a.building,a.room].map(campusText).filter(Boolean).join(' · ')||'地点未提供'));
    if(a.teachers)card.append(element('p',campusText(a.teachers)));
    if(!week)card.append(element('small','周次：'+(a.weeks_text||(a.weeks||[]).join('、')||'未提供')));
    if(a.group!==null&&a.group!==undefined&&a.group!=='')card.append(element('small','教学分组：'+campusText(a.group)));
    if(a.self_defined_dates)card.append(element('small','含自定义日期，请核对原站','attention'));
    if(week&&(!Array.isArray(a.weeks)||!a.weeks.length))card.append(element('small','周次未明确，请核对原站','attention'));
    return card;
  }
  for(let day=1;day<=7;day++){
    const column=element('section',undefined,'week-day');column.append(element('h3',['周一','周二','周三','周四','周五','周六','周日'][day-1]));
    const daily=rows.filter(a=>a.weekday===day).sort((a,b)=>(a.start_period??99)-(b.start_period??99)||String(a.start||'').localeCompare(String(b.start||'')));
    for(const a of daily)column.append(lesson(a));if(!daily.length)column.append(element('p','无已知课程','muted'));board.append(column);
  }
  const unknown=rows.filter(a=>!Number.isInteger(a.weekday)||a.weekday<1||a.weekday>7);
  if(unknown.length){unplaced.append(element('h3','待核对安排'));for(const a of unknown)unplaced.append(lesson(a));}
}
$('siteSearch').oninput=renderSites;
$('profileForm').oninput=()=>{campusDirty=true;$('profileStatus').textContent='有未保存的修改。';};$('profileForm').onsubmit=saveCampusProfile;
$('reloadProfile').onclick=async()=>{if(!campusDirty||await confirmAction('重新读取资料','当前未保存的修改会被替换。'))loadProfile(true);};
$('timetableSemester').onchange=renderTimetable;$('timetableWeek').onchange=renderTimetable;
$('refreshTimetable').onclick=()=>operation('/api/campus/timetable/refresh',{semester_id:Number($('timetableSemester').value)});
$('timetableConnection').onclick=()=>show('accounts');
$('openTimetable').onclick=()=>show('timetable');
renderControls();refresh();const pollTimer=setInterval(refresh,3000);
