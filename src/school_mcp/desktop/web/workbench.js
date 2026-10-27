'use strict';
// One shared snapshot feeds both course and task views; personal records live in Rust.
const Workbench=(()=>{
 let data={courses:[],tasks:[],revision:0}, timetable=null, courseId='', taskId='', tab='overview', term='', search='', filter='pending', courseFilter='', assistant=false, contextKey='', dirty=false, loading=false, saving=false, generation=0, handled='', events=[], chats=[], selectedContext=null;
 const labels={not_started:'未开始',in_progress:'进行中',completed:'已完成'}, platform={not_checked:'尚未核实',unknown:'待核实',historical_attempt_present:'有提交记录',not_applicable:'手动任务'};
 const e=element,b=button;
 const root=()=>$(currentView==='courses'?'courseWorkspace':'assignmentWorkspace');
 const current=()=>data.courses.find(c=>c.id===courseId);
 const courseName=id=>data.courses.find(c=>c.id===id)?.title||'个人任务';
 const urlFor=t=>t.source==='bb'&&t.entry_path?'https://www.bb.ustc.edu.cn'+t.entry_path:null;
 function link(title,url){const a=safeSourceLink(url);if(a)a.textContent=title;return a||e('span',title);}
 function note(s){return e('p',s,'study-muted');}
 function option(value,label){const o=e('option',label);o.value=value;return o;}
 function select(items,value,change){const s=e('select');s.append(...items.map(([v,l])=>option(v,l)));s.value=value;s.onchange=()=>change(s.value);return s;}
 function field(label,type,value=''){const wrap=e('label',label,'study-field'),input=e(type==='textarea'?'textarea':'input');if(type!=='textarea')input.type=type;input.value=value;wrap.append(input);return [wrap,input];}
 function actions(...nodes){const n=e('div',undefined,'actions');n.append(...nodes);return n;}
 function localDate(s){return s?new Date(s).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):'';}
 function due(t){return t.due_at?'截止 '+localDate(t.due_at):(t.due_text||[]).join(' · ')||'截止时间未注明';}
 function urgent(t){return t.progress!=='completed'&&!t.archived&&(t.changed||(t.due_at&&new Date(t.due_at)-Date.now()<48*3600000));}
 function counts(cid){const all=data.tasks.filter(t=>(!cid||t.course_id===cid)&&!t.archived);return {pending:all.filter(t=>t.progress!=='completed').length,urgent:all.filter(urgent).length,done:all.filter(t=>t.progress==='completed').length};}
 function matchTerm(c){return !term||c.title.includes(term);}
 async function load(){
  const g=++generation,session=sessionRevision;if(!loggedIn()){data={courses:[],tasks:[],revision:0};render();return;}
  loading=true;status('正在读取已保存的课程…');
  try{const [v,tt,ev,ch]=await Promise.all([api('/api/study'),api('/api/campus/timetable').catch(()=>null),api('/api/schedule').catch(()=>null),api('/api/chats').catch(()=>({chats:[]}))]);if(g!==generation||session!==sessionRevision||!loggedIn())return;data=v;timetable=tt;events=ev?.events||[];chats=ch.chats||[];if(!dirty)render();else status('后台数据已更新，当前编辑保留；保存前请核对。');}
  catch(x){status(x.message);}
  finally{loading=false;}
 }
 function status(s){const n=$('studyStatus');if(n)n.textContent=s;}
 async function mutate(values){if(saving)return false;saving=true;const session=sessionRevision;try{const v=await api('/api/study',{revision:data.revision,...values});if(session!==sessionRevision)return false;data=v;dirty=false;render();return true;}catch(x){toast(x.message);return false;}finally{saving=false;}}
 async function refreshRemote(scope,extra={}){if(dirty){toast('请先保存任务，再更新学校记录。');return;}if(busy()){toast('当前有操作进行中，请完成后再更新。');return;}try{await operation('/api/study/refresh',{scope,...extra});}catch(x){toast(x.message);}}
 function leave(){if(dirty&&!confirm('有尚未保存的修改，放弃这些修改？'))return false;dirty=false;return true;}
 function openCourse(id){if(!leave())return;courseId=id;taskId='';tab='overview';show('courses');}
 function openTask(id){if(!leave())return;taskId=id;render();}
 async function openRelatedTask(id){if(!leave())return;await load();if(!data.tasks.some(t=>t.id===id)){toast('未找到这项关联任务，可继续打开原作业链接。');return;}taskId=id;filter='all';search='';term='';courseFilter='';show('assignments');}
 function toolbar(host){
  const tools=e('div',undefined,'study-toolbar'),input=e('input');input.type='search';input.placeholder=currentView==='courses'?'搜索课程':'搜索作业或课程';input.setAttribute('aria-label',input.placeholder);input.value=search;input.oninput=()=>{if(dirty){toast('请先保存或关闭当前任务编辑。');return;}search=input.value;taskId='';renderBody();};
  const terms=[...new Set(data.courses.flatMap(c=>c.title.match(/20\d{2}(?:FA|SP|SU|WI)/g)||[]))].sort().reverse();
  const termSelect=select([['','全部学期'],...terms.map(x=>[x,x])],term,v=>{if(!leave())return;term=v;courseId='';taskId='';render();});termSelect.setAttribute('aria-label','课程学期');
  tools.append(input,termSelect,b('更新课程列表',()=>refreshRemote('courses'),''),b('+ 添加任务',()=>manualForm(),'primary'));
  host.append(tools);const s=note(data.updated_at?'课程列表更新于 '+dateText(data.updated_at)+' · 点击课程更新作业和材料':'首次使用请更新课程列表，也可以先添加个人任务。');s.id='studyStatus';host.append(s);
 }
 function render(){if(!['courses','assignments'].includes(currentView))return;const host=root();$(currentView==='courses'?'assignmentWorkspace':'courseWorkspace').replaceChildren();host.replaceChildren();if(!loggedIn()){host.append(note('登录学校身份后查看课程和作业。'));return;}toolbar(host);const body=e('div',undefined,'study-body');body.id='studyBody';host.append(body);renderBody();}
 function renderBody(){const body=$('studyBody');if(!body)return;body.replaceChildren();if(currentView==='courses'){if(courseId&&current())courseView(body,current());else courseGrid(body);}else taskListView(body);if(taskId){const t=data.tasks.find(t=>t.id===taskId);if(t)taskDetail(body,t);}}
 function courseGrid(host){
  const n=counts();const summary=e('div',undefined,'study-summary');summary.append(e('strong',`${data.courses.length} 门课程`),e('span',`${n.pending} 项待办`),e('span',`${n.urgent} 项需留意`));host.append(summary);
  const grid=e('div',undefined,'study-course-grid');host.append(grid);
  for(const c of [...data.courses].filter(c=>matchTerm(c)&&c.title.toLowerCase().includes(search.toLowerCase())).sort((a,b)=>Number(b.pinned)-Number(a.pinned)||a.title.localeCompare(b.title))){
   const card=e('article',undefined,'study-course-card'),open=b(c.title,()=>openCourse(c.id),'study-course-title');const n=counts(c.id);card.append(open,note(`${n.pending} 项待办 · ${n.urgent} 项需留意`));
   card.append(note(c.checked_at?'内容更新 '+dateText(c.checked_at):'尚未读取作业与材料'));
   if(c.errors&&Object.keys(c.errors).length)card.append(badge('部分内容待更新','attention'));
   card.append(actions(b(c.pinned?'取消置顶':'置顶',()=>mutate({action:'course',id:c.id,pinned:!c.pinned})),b('进入课程',()=>openCourse(c.id))));grid.append(card);
  }
  if(!grid.children.length)grid.append(note(data.courses.length?'没有匹配的课程。':'更新课程列表后，课程会出现在这里。'));
 }
 function courseView(host,c){
  host.append(b('← 全部课程',()=>{if(leave()){courseId='';render();}}));
  const heading=e('div',undefined,'study-heading');heading.append(e('h2',c.title),actions(link('打开课程网站','https://www.bb.ustc.edu.cn'+c.path),b('更新本课程',()=>refreshRemote('course',{course_id:c.id})),b('问这门课',()=>ask(c,null,''),'primary')));host.append(heading);
  host.append(note(c.checked_at?'内容更新于 '+dateText(c.checked_at):'点击“更新本课程”读取作业、材料入口和通知。'));if(c.catalog?.truncated)host.append(note('本次作业目录只读取了一部分，完整作业请同时核对原课程网站。'));
  for(const [key,msg] of Object.entries(c.errors||{}))host.append(note(({page:'主页',catalog:'作业',announcements:'通知'}[key]||key)+'：'+msg));
  const nav=e('div',undefined,'study-tabs');for(const [key,label] of [['overview','概览'],['tasks','作业'],['materials','材料'],['notices','通知']])nav.append(b(label,()=>{if(leave()){tab=key;taskId='';render();}},tab===key?'selected':''));host.append(nav);
  if(tab==='tasks'){taskRows(host,data.tasks.filter(t=>t.course_id===c.id&&!t.archived));return;}
  if(tab==='materials'){materials(host,c);return;}
  if(tab==='notices'){host.append(e('pre',c.announcements?.text||'尚未读取通知。','study-prose'));if(c.announcements?.text_truncated)host.append(note('当前文字已截取，请打开原文查看全部。'));if(c.announcements?.url)host.append(link('打开通知原文',c.announcements.url));return;}
  const panels=e('div',undefined,'study-overview');host.append(panels);
  const lessons=e('section',undefined,'study-card');lessons.append(e('h3','上课安排'));panels.append(lessons);timetableSection(lessons,c);
  const upcoming=e('section',undefined,'study-card');upcoming.append(e('h3','待处理作业'));panels.append(upcoming);taskRows(upcoming,data.tasks.filter(t=>t.course_id===c.id&&!t.archived&&t.progress!=='completed').slice(0,5));
  const related=chats.filter(x=>x.study_context?.kind==='course'&&x.study_context.id===c.id);if(related.length){host.append(e('h3','相关对话'));for(const x of related)host.append(b(x.title,()=>ask(c,null,'',x.id)));}
  host.append(e('h3','课程更新'),e('pre',(c.announcements?.text||'更新本课程后显示通知。').slice(0,1200),'study-prose'));
 }
 function timetableSection(host,c){
  const tt=timetable?.data,rows=tt?.activities||[],names=[...new Set(rows.map(x=>x.course_name).filter(x=>typeof x==='string'))];
  host.append(note(timetable?.updated_at?'课表快照 '+dateText(timetable.updated_at):'先在“我的课表”中更新课表，再关联课程。'));
  const sel=select([['','选择对应教务课程'],...names.map(n=>[n,n])],c.timetable_key||'',v=>mutate({action:'course',id:c.id,timetable_key:v}));sel.setAttribute('aria-label','关联教务课程');host.append(sel);
  const mapped=rows.filter(x=>x.course_name===c.timetable_key);for(const x of mapped){const day=Number.isInteger(x.weekday)&&x.weekday>=1&&x.weekday<=7?'周'+'一二三四五六日'[x.weekday-1]:'日期待核对';const time=x.start&&x.end?x.start+'–'+x.end:x.start_period&&x.end_period?`第 ${x.start_period}–${x.end_period} 节`:'时间待核对';host.append(note(`${day} · ${time} · ${[x.campus,x.building,x.room].map(campusText).filter(Boolean).join(' ')||'地点待核对'} · ${x.weeks_text||(x.weeks?.length?'周次 '+x.weeks.join('、'):'周次待核对')}`));}
  host.append(b('打开我的课表',()=>show('timetable')));
 }
 function materials(host,c){
  host.append(actions(b('添加个人材料链接',()=>materialForm(c)),b('带材料问助手',()=>{ask(c,null,'请先查看这门课程的材料目录。我要通过“添加材料”选择本机文件，再请你阅读。');})));host.append(note('选择目录读取文件列表；文件可在原网站打开、下载，并通过助手的“添加材料”阅读。'));
  const pages=[c.page,...(c.material_pages||[])].filter(Boolean),seen=new Set();
  for(const page of pages){const section=e('section',undefined,'study-card');section.append(e('h3',page.title||'材料目录'));for(const l of page.links||[]){if(seen.has(l.url)||!l.text)continue;seen.add(l.url);const row=e('div',undefined,'study-material');row.append(link(l.text,l.url));if(l.internal&&l.path?.startsWith('/webapps/blackboard/content/listContent.jsp'))row.append(b('读取目录',()=>refreshRemote('page',{course_id:c.id,path:l.path})));section.append(row);}host.append(section);}
  for(const l of c.personal_materials||[])host.append(actions(link(l.title,l.url),note('本人添加')));
  if(!pages.length)host.append(note('点击“更新本课程”获取材料入口。'));
 }
 function taskListView(host){
  const filters=e('div',undefined,'study-tabs');for(const [key,label] of [['pending','待处理'],['completed','已完成'],['all','全部'],['archived','已归档']])filters.append(b(label,()=>{if(leave()){filter=key;taskId='';render();}},filter===key?'selected':''));
  const sel=select([['','全部课程'],...data.courses.filter(matchTerm).map(c=>[c.id,c.title])],courseFilter,v=>{if(leave()){courseFilter=v;render();}});sel.setAttribute('aria-label','筛选课程');filters.append(sel);host.append(filters);
  let tasks=data.tasks.filter(t=>(filter==='archived'?t.archived:!t.archived)&&(!courseFilter||t.course_id===courseFilter)&&(!term||data.courses.some(c=>c.id===t.course_id&&matchTerm(c)))&&(`${t.title} ${courseName(t.course_id)}`).toLowerCase().includes(search.toLowerCase()));
  if(filter==='pending')tasks=tasks.filter(t=>t.progress!=='completed');if(filter==='completed')tasks=tasks.filter(t=>t.progress==='completed');
  for(const [label,items] of [['需留意',tasks.filter(urgent)],['接下来',tasks.filter(t=>!urgent(t)&&t.due_at)],['未注明截止时间',tasks.filter(t=>!urgent(t)&&!t.due_at)]])if(items.length){host.append(e('h3',label));taskRows(host,items);}
  if(!tasks.length)host.append(note('当前筛选下没有任务。可以更新课程作业，或手动添加任务。'));
 }
 function taskRows(host,tasks){
  const list=e('div',undefined,'study-task-list');host.append(list);
  for(const t of [...tasks].sort((a,b)=>(a.due_at||'9999').localeCompare(b.due_at||'9999'))){
   const row=b('',()=>openTask(t.id),'study-task-row');const name=e('div');name.append(e('strong',t.title),e('small',courseName(t.course_id)));const state=e('div');state.append(badge(labels[t.progress]||'未开始',t.progress==='completed'?'good':''),e('small',platform[t.platform_state]||'待核实'));
   row.append(name,e('span',due(t),urgent(t)?'study-attention':''),state);if(t.changed)row.append(badge('要求有更新','attention'));list.append(row);
  }
  if(!tasks.length)list.append(note('当前没有待处理作业。'));
 }
 function taskDetail(host,t){
  const d=e('section',undefined,'study-detail');d.id='studyTaskDetail';d.setAttribute('aria-label','作业详情');host.append(d);
  d.append(actions(b('关闭详情',()=>{if(leave()){taskId='';render();}}),b('问助手',()=>ask(data.courses.find(c=>c.id===t.course_id),t,''),'primary')),e('h2',t.title),note(courseName(t.course_id)),note(due(t)));
  if(t.source==='bb')d.append(actions(link('打开原作业 / 提交页面',urlFor(t)),b('核实提交记录',()=>refreshRemote('assignment',{course_id:t.course_id,content_id:t.content_id}))));
  d.append(note('平台记录：'+(platform[t.platform_state]||'待核实')+(t.verified_at?' · 核实于 '+dateText(t.verified_at):'')));
  if(t.verification_error){const n=note(t.verification_error.message);n.classList.add('study-verification-error');n.setAttribute('role','status');d.append(n);}
  if(t.platform_state==='unknown')d.append(note('本次页面尚未提供可确认的提交状态，请打开原作业页面查看。'));
  if(t.verification?.continue_control_present)d.append(note('原网站提供“继续”入口，可打开原作业页面查看尚待处理的内容。'));
  if(t.verification?.attempt_time_text)d.append(note('当前显示尝试：'+t.verification.attempt_time_text));
  if(t.not_in_latest)d.append(note('本次目录中未找到此作业，已保留原记录与个人进度。'));
  if(t.changed){const changed=e('details');changed.append(e('summary','查看要求变化'));for(const x of t.changes||[])changed.append(e('p',({title:'标题',description:'要求',due_text:'截止时间'}[x.field]||x.field)+'：'),e('pre',String(Array.isArray(x.before)?x.before.join('\n'):x.before),'study-prose'),e('pre','更新为：'+String(Array.isArray(x.after)?x.after.join('\n'):x.after),'study-prose'));d.append(changed,b('已查看更新',()=>{if(leave())mutate({action:'update',id:t.id,acknowledge:true});}));}
  const source=e('details');source.append(e('summary','原始要求'),e('pre',t.description||'原平台未提供要求正文。','study-prose'));d.append(source);if(t.description_truncated)d.append(note('要求已截取，完整内容见原作业页面。'));
  const form=e('form',undefined,'study-edit');let editRevision=data.revision;form.oninput=()=>{dirty=true;};
  const progress=select(Object.entries(labels),t.progress,()=>{dirty=true;});progress.setAttribute('aria-label','我的进度');form.append(e('label','我的进度'),progress);
  const [nw,notes]=field('个人备注','textarea',t.notes||'');notes.maxLength=8000;form.append(nw);
  const [pw,planned]=field('个人计划完成时间（北京时间）','datetime-local',toInput(t.planned_at));form.append(pw);
  form.append(e('h3','交付清单'),note('原文要求和个人建议可分别记录；每项保留依据。'));
  const entries=e('div');form.append(entries);const checks=[];
  function addCheck(x={text:'',done:false,source:'本人整理'}){const r=e('div',undefined,'study-check');const done=e('input');done.type='checkbox';done.checked=x.done;done.setAttribute('aria-label','此项已完成');const text=e('input');text.value=x.text;text.placeholder='需要完成的事项';text.maxLength=500;const source=e('input');source.value=x.source||'';source.placeholder='原文摘录、页码，或“个人建议”';source.maxLength=1000;const record={r,done,text,source};checks.push(record);r.append(done,text,source,b('移除',()=>{checks.splice(checks.indexOf(record),1);r.remove();dirty=true;}));entries.append(r);}
  for(const x of t.checklist||[])addCheck(x);
  form.append(actions(b('+ 清单事项',()=>{addCheck();dirty=true;}),b('让助手整理要求',()=>ask(data.courses.find(c=>c.id===t.course_id),t,'请根据这项作业的原始要求整理交付清单，每项注明原文依据；未读取的附件请先读取。整理好后让我确认，再保存到这项任务。'))));
  const save=b('保存任务',()=>{},'primary');save.type='submit';form.append(save);
  form.onsubmit=async ev=>{ev.preventDefault();if(editRevision!==data.revision){toast('后台记录已更新，请先复制当前修改，关闭详情后重新打开再保存。');return;}save.disabled=true;try{await mutate({action:'update',id:t.id,progress:progress.value,notes:notes.value,planned_at:fromInput(planned.value),checklist:checks.map(x=>({text:x.text.value,done:x.done.checked,source:x.source.value}))});}finally{save.disabled=false;}};d.append(form);
  d.append(actions(b('加入日程',()=>scheduleForm(t)),b(t.archived?'恢复任务':'归档任务',()=>{if(leave())mutate({action:'archive',id:t.id,archived:!t.archived});})));
  for(const x of chats.filter(x=>x.study_context?.kind==='task'&&x.study_context.id===t.id))d.append(b('继续对话：'+x.title,()=>ask(data.courses.find(c=>c.id===t.course_id),t,'',x.id)));
  if(t.source==='manual')d.append(b('编辑任务信息',()=>manualForm(t)));
  const related=events.filter(x=>x.notes?.includes('[任务 '+t.id+']'));for(const x of related)d.append(b('日程：'+localDate(x.start)+' '+x.title,()=>{$('scheduleDate').value=calendarDay(x.start);show('schedule');showScheduleDetail(x);}));
 }
 function toInput(s){if(!s)return '';const d=new Date(new Date(s).getTime()+8*3600000);return d.toISOString().slice(0,16);}
 function fromInput(s){return s?s+':00+08:00':null;}
 function dialog(title){if(!leave())return null;const d=e('dialog',undefined,'study-dialog');const h=e('h2',title),f=e('form');d.append(h,f);document.body.append(d);d.addEventListener('close',()=>{dirty=false;d.remove();});d.showModal();return {d,f};}
 function manualForm(t=null){const box=dialog(t?'编辑任务':'添加任务');if(!box)return;const {d,f}=box;const cid=select([['','个人任务'],...data.courses.map(c=>[c.id,c.title])],t?.course_id||courseId,()=>{});if(t)cid.disabled=true;f.append(e('label','所属课程'),cid);const [tw,title]=field('任务名称','text',t?.title||''),[dw,description]=field('任务要求','textarea',t?.description||''),[uw,dueAt]=field('截止时间（北京时间，可留空）','datetime-local',toInput(t?.due_at));title.required=true;title.maxLength=300;description.maxLength=12000;f.append(tw,dw,uw);const save=b('保存',()=>{},'primary');save.type='submit';f.append(actions(b('取消',()=>d.close()),save));f.onsubmit=async ev=>{ev.preventDefault();save.disabled=true;const ok=await mutate({action:t?'update':'create',id:t?.id,title:title.value,course_id:cid.value,description:description.value,due_at:fromInput(dueAt.value)});save.disabled=false;if(ok)d.close();};}
 function materialForm(c){const box=dialog('添加个人材料链接');if(!box)return;const {d,f}=box;const [tw,title]=field('材料名称','text'),[uw,url]=field('HTTPS 链接','url');title.required=url.required=true;f.append(tw,uw);const save=b('保存',()=>{},'primary');save.type='submit';f.append(actions(b('取消',()=>d.close()),save));f.onsubmit=async ev=>{ev.preventDefault();save.disabled=true;const ok=await mutate({action:'material',id:c.id,title:title.value,url:url.value});save.disabled=false;if(ok)d.close();};}
 function scheduleForm(t){const box=dialog('安排作业时间');if(!box)return;const {d,f}=box;const [sw,start]=field('开始时间（北京时间）','datetime-local'),[ew,end]=field('结束时间（北京时间）','datetime-local'),[rw,reminder]=field('提前提醒（分钟）','number','10');start.required=end.required=true;reminder.min=0;reminder.max=10080;f.append(sw,ew,rw,note('应用运行时提醒；与本任务关联，原作业截止时间保持不变。'));const save=b('创建日程',()=>{},'primary');save.type='submit';f.append(actions(b('取消',()=>d.close()),save));f.onsubmit=async ev=>{ev.preventDefault();save.disabled=true;try{await api('/api/schedule',{action:'create',events:[{title:t.title,start:fromInput(start.value),end:fromInput(end.value),reminder_minutes:Number(reminder.value),notes:'[任务 '+t.id+']\n'+courseName(t.course_id),links:urlFor(t)?[{title:t.title,url:urlFor(t)}]:[]}]});d.close();await load();toast('日程已保存。');}catch(x){toast(x.message);}finally{save.disabled=false;}};}
 async function ask(c,t,prompt,chatId=null){
  if(!leave())return;if(busy()&&!assistant){toast('请先完成当前对话，再打开课程助手。');return;}
  const key=t?.id||c?.id||'study';if(chatId){selectChat(chatId);await loadChat(chatId);}else if(contextKey!==key||!assistant){selectChat(null);}contextKey=key;selectedContext={kind:t?'task':'course',id:t?.id||c.id};
  assistant=true;document.body.classList.add('study-assistant-open');const panel=$('studyAssistant');panel.classList.remove('hidden');$('studyAssistantTitle').textContent=t?.title||c?.title||'课程助手';
  panel.append($('view-chat'));$('view-chat').classList.remove('hidden');$('onboarding').classList.add('hidden');$('welcome').classList.add('hidden');
  if(prompt)$('messageInput').value=prompt;
  else if(!chatId)$('messageInput').value='';
  $('messageInput').focus();
 }
 function closeAssistant(){const wasOpen=assistant;assistant=false;selectedContext=null;document.body.classList.remove('study-assistant-open');$('studyAssistant').classList.add('hidden');document.querySelector('main').append($('view-chat'));$('view-chat').classList.toggle('hidden',currentView!=='chat');if(wasOpen&&activeChat&&loggedIn())load();}
 function open(view){if(!['courses','assignments'].includes(view)){if(assistant)closeAssistant();return;}if(assistant)$('view-chat').classList.remove('hidden');render();load();}
 function reset(){generation++;data={courses:[],tasks:[],revision:0};courseId=taskId=contextKey='';dirty=false;timetable=null;events=[];chats=[];closeAssistant();document.querySelectorAll('.study-dialog').forEach(x=>x.close());render();}
 function tick(s,changed){if(!loggedIn())return;if(changed&&s.operation.kind==='study_refresh'&&!s.busy)load();if(changed&&s.operation.kind==='chat'&&!s.busy)load();}
 return {open,load,reset,tick,closeAssistant,leave,openRelatedTask,get context(){return assistant?selectedContext:null;},get assistant(){return assistant;},get dirty(){return dirty;}};
})();
$('studyAssistantClose').onclick=Workbench.closeAssistant;
