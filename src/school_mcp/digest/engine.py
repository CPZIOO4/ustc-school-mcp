"""Deterministic digest state machine. No model or webpage can authorize delivery."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid
from .store import Store, defaults
from .sources import catalog, collect
from .calendar import parse_time, CST
from ..safety_log import record
from . import native

def _native_store(action, store, **values):
    from .store import owner
    return native.call(action, root=native.directory(store.root), owner=owner(), **values)

def utcnow():return datetime.now(timezone.utc)

def fingerprint(item):
    if native.enabled():return native.call('fingerprint',item=item)
    stable={k:v for k,v in item.items() if k not in {
        'collected_at','revision','change','matched_by','last_seen','fingerprint','urgent_key','detail_unavailable'}}
    if stable.get('poster_ocr'):stable['poster_ocr']={k:v for k,v in stable['poster_ocr'].items() if k!='errors'}
    return hashlib.sha256(json.dumps(stable,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def settings(value):
    if native.enabled():return native.call('settings',value=value)
    base=defaults()
    if not isinstance(value,dict) or set(value)-set(base):raise ValueError('订阅设置字段无效。')
    base.update(value)
    for k in ['enabled','email_enabled']:
        if type(base[k]) is not bool:raise ValueError('订阅开关须为布尔值。')
    valid={x['id'] for x in catalog()}
    if not isinstance(base['sources'],list) or not 1<=len(base['sources'])<=12 or any(x not in valid for x in base['sources']):
        raise ValueError('选择 1–12 个已适配的信息源。')
    base['sources']=list(dict.fromkeys(base['sources']))
    for key in ['keywords','exclude','locations','bb_courses']:
        if not isinstance(base[key],list) or len(base[key])>20 or any(not isinstance(x,str) or not 1<=len(x.strip())<=100 for x in base[key]):
            raise ValueError('筛选项最多 20 个，每项 1–100 字。')
        base[key]=list(dict.fromkeys(x.strip() for x in base[key]))
    if any(not re.fullmatch(r'_?\d+(?:_\d+)?',x) for x in base['bb_courses']):
        raise ValueError('请使用 BB 课程查询返回的编号。')
    if 'bb' in base['sources'] and not base['bb_courses']:raise ValueError('订阅 BB 作业前请选择课程。')
    for k,lo,hi in [('max_pages',1,10),('detail_limit',0,30),('interval_minutes',15,1440),('urgent_hours',0,168)]:
        if type(base[k]) is not int or not lo<=base[k]<=hi:raise ValueError('采集范围或提醒间隔超出支持范围。')
    if not isinstance(base['daily_time'],str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',base['daily_time']):
        raise ValueError('日报时间使用北京时间 HH:MM。')
    from ..mail.outbox import valid_address
    if not isinstance(base['email'],str) or (base['email'] and not valid_address(base['email'])):
        raise ValueError('请输入有效的通知收件邮箱。')
    if base['email_enabled'] and not base['email']:raise ValueError('邮件推送需要收件邮箱。')
    return base

def configure(value, authorized=False, store=None):
    store=store or Store();value=settings(value)
    if value['enabled'] and not authorized:
        raise ValueError('开启定时采集与推送前，需要用户确认来源、时间和收件人。')
    if native.enabled():
        sender=None
        if value['enabled'] and value['email_enabled']:
            from ..mail.config import load_config
            sender=load_config().address
        return _native_store('configure',store,settings=value,authorized=authorized,sender=sender)
    with store.cycle():
        state=store.read();old=state['settings']
        if value['enabled'] and value['email_enabled']:
            from ..mail.config import load_config
            sender=load_config().address
        else:sender=state.get('sender')
        # Freeze sender too. Changing school/mail identities must not silently move consent.
        state['sender']=sender
        if any(old.get(k)!=value.get(k) for k in ['sources','keywords','exclude','locations','bb_courses']) or value['enabled'] and not old['enabled']:
            state['sources']={};state['pending']={};state['last_cycle']=None
        state['settings']=value;state['configured_at']=utcnow().isoformat();state['subscription_version']=uuid.uuid4().hex;store.save(state)
    return {'settings':value,'state':'enabled' if value['enabled'] else 'paused','baseline':'首次成功采集建立基线，不推送全部历史记录。'}

def matches(x, cfg):
    if native.enabled():return native.call('matches',item=x,settings=cfg)
    hay=' '.join(str(x.get(k) or '') for k in ['title','summary','company','location','major','education']).casefold()
    if any(w.casefold() in hay for w in cfg['exclude']):return []
    if cfg['locations'] and not any(w.casefold() in (x.get('location') or '').casefold() for w in cfg['locations']):return []
    hit=[w for w in cfg['keywords'] if w.casefold() in hay]
    return hit or (['所选来源'] if not cfg['keywords'] else [])

def deduplicate(items):
    if native.enabled():return native.call('deduplicate',items=list(items))
    result=[];seen={}
    for x in items:
        # Merge exact content/title/date cross-posts, never fuzzy-merge similar activities.
        key=(re.sub(r'\s+','',x['title']),x.get('published_at'),x.get('start'),x.get('deadline'),x.get('summary'))
        if key in seen and x.get('summary') and x.get('published_at'):
            seen[key].setdefault('also_from',[]).append(x['source_url']);continue
        copy=dict(x);seen[key]=copy;result.append(copy)
    return result

def report_text(events, health):
    if native.enabled():return native.call('report_text',items=list(events),health=health)
    lines=['校园信息简报（北京时间）','']
    events=deduplicate(events);shown=0
    for x in events:
        if shown>=50 or sum(len(s) for s in lines)>14000:break
        shown+=1
        kind={'new':'新增','changed':'信息变更','deadline':'即将截止','source_failed':'来源异常'}.get(x.get('change'),'事项')
        lines.append(f'【{kind}】{x["title"]}')
        if x.get('deadline'):lines.append('截止：'+x['deadline'])
        if x.get('start'):lines.append('时间：'+x['start'])
        if x.get('location'):lines.append('地点：'+x['location'])
        if x.get('summary'):lines.append(x['summary'][:350])
        if x.get('matched_by'):lines.append('匹配：'+'、'.join(x['matched_by']))
        lines+=['来源：'+x['source_url'],'']
    if shown<len(events):lines.append(f'本简报共 {len(events)} 项，此处显示 {shown} 项；其余请在信息中心缩小来源与关键词查看。')
    incomplete=[k for k,v in health.items() if v.get('state')!='ok' or not v.get('complete')]
    if incomplete:lines.append('未完整覆盖：'+'、'.join(incomplete)+'。请在信息中心查看最后成功时间与范围。')
    return '\n'.join(lines)

def _deliver(state, message, store, sender=None):
    cfg=state['settings']
    if not cfg['email_enabled']:
        message['delivery']={'status':'app_only'};return
    from ..mail.config import load_config
    from ..mail import outbox
    mail=load_config()
    if mail.address!=state.get('sender'):
        message['delivery']={'status':'blocked_sender_changed'};return
    # Persist the logical notification and exact outbox draft before SMTP. Never
    # create a replacement draft after any uncertain attempt, even across restart.
    if message.get('subscription_version')!=state.get('subscription_version'):
        message['delivery']['status']='superseded'
        return
    original=message.get('delivery',{}).get('draft_id')
    if original:
        draft=outbox.status(original)
        if draft['status']!='ready':
            message['delivery'].update(status=draft['status'],server_accepted=draft.get('server_accepted',False));return
    else:
        draft=outbox.prepare(mail,to=[cfg['email']],subject='校园信息简报',body=message['text'])
    message['delivery']={'status':'prepared','draft_id':draft['draft_id'],'content_sha256':draft['content_sha256']}
    store.save(state)
    message['delivery']['status']='sending';store.save(state)
    result=(sender or outbox.send)(mail,draft['draft_id'],draft['content_sha256'])
    message['delivery'].update(status=result.get('status','unknown'),server_accepted=result.get('server_accepted',False))

def refresh(*, notify=False, store=None, collector=collect, now=None, sender=None):
    store=store or Store();now=now or utcnow()
    if native.enabled():
        return _refresh_native(notify=notify,store=store,collector=collector,now=now,sender=sender)
    with store.cycle():
        state=store.read();cfg=state['settings']
        notify=notify and cfg['enabled']
        if notify and state.get('last_cycle') and now-datetime.fromisoformat(state['last_cycle'])<timedelta(minutes=cfg['interval_minutes']):
            return {'state':'not_due','last_cycle':state['last_cycle']}
        baseline=[]
        if notify:
            for message in state['messages']:
                if message.get('delivery',{}).get('status') in {'pending','prepared','sending'}:
                    try:_deliver(state,message,store,sender)
                    except Exception as exc:
                        message['delivery'].update(status='unknown',**record(service='digest',stage='background',exc=exc,outcome='unknown'))
        for source in cfg['sources']:
            previous=state['sources'].get(source,{})
            if previous.get('retry_at') and now<datetime.fromisoformat(previous['retry_at']):continue
            try:
                response=collector(source,cfg)
                if not isinstance(response,dict) or not isinstance(response.get('items'),list):raise ValueError('信息源返回格式不正确。')
                new_baseline=not previous.get('initialized')
                if new_baseline:baseline.append(source)
                for x in response['items']:
                    if x.get('poster_ocr'):
                        # Full OCR remains available on detail read, not duplicated in every stored report.
                        x={**x,'poster_ocr':{**x['poster_ocr'],'images':[{k:v for k,v in row.items() if k!='text'} for row in x['poster_ocr']['images']]}}
                    if len(x.get('summary',''))>1200:
                        x=dict(x,summary=x['summary'][:1200],summary_truncated=True)
                    old=state['items'].get(x['id'])
                    if old and x.get('poster_ocr',{}).get('errors') and old.get('poster_ocr',{}).get('images'):
                        x={**x,'summary':old.get('summary',''),'poster_ocr':old['poster_ocr'],
                           'time_candidates':old.get('time_candidates',{}),'detail_unavailable':True,
                           'summary_truncated':old.get('summary_truncated',False)}
                    # A transient detail failure must not erase known detail or make a false change.
                    if old and not x.get('summary') and old.get('summary'):
                        x={**old,**{k:v for k,v in x.items() if v not in ('',None,{})}}
                    fp=fingerprint(x);changed=old is not None and old['fingerprint']!=fp
                    x.update(fingerprint=fp,revision=(old.get('revision',0)+1 if changed else old.get('revision',0)) if old else 0,
                             last_seen=now.isoformat(),matched_by=matches(x,cfg),urgent_key=old.get('urgent_key') if old else None)
                    state['items'][x['id']]=x
                    if cfg['enabled'] and not new_baseline and (old is None or changed) and x['matched_by']:
                        state['pending'][x['id']]={**x,'summary':x.get('summary','')[:450],'change':'changed' if changed else 'new'}
                state['sources'][source]=dict(state='ok',initialized=True,last_success=now.isoformat(),
                    complete=response.get('complete',False),scope=response.get('scope'),next_page=response.get('next_page'),
                    detail_failures=response.get('detail_failures',0),failures=0)
                state['sources'][source]['excluded_low_quality']=response.get('excluded_low_quality',0)
                state['sources'][source]['unsupported_links']=response.get('unsupported_links',0)
                if previous.get('state')=='failed':state['sources'][source]['recovered_at']=now.isoformat()
            except Exception as exc:
                failures=previous.get('failures',0)+1
                incident=record(service='digest',stage='background',exc=exc,outcome='read_failed')
                from ..recovery import classify
                error_code,_=classify(exc)
                state['sources'][source]={**previous,'state':'failed','failures':failures,'error_id':incident['error_id'],
                    'error_code':error_code,'next_action':'在账号页检查该平台登录' if error_code in {'authentication_required','identity_required','manual_verification_required'} else '检查来源状态，等待冷却后重试',
                    'retry_at':(now+timedelta(minutes=min(1440,cfg['interval_minutes']*2**min(failures-1,5)))).isoformat()}
                if cfg['enabled'] and previous.get('state')!='failed':
                    state['pending']['error:'+source]=dict(id='error:'+source,title=source+' 暂时读取失败',source=source,
                        source_url='',summary='本次未检查成功，不能据此判断没有新消息。错误编号：'+incident['error_id'],change='source_failed')
        # Don't preserve expired content forever. Bound storage, report any truncation.
        keep=sorted(state['items'].values(),key=lambda x:x['last_seen'],reverse=True)
        state['storage_truncated']=len(keep)>1500
        state['items']={x['id']:x for x in keep[:1500] if now-datetime.fromisoformat(x['last_seen'])<timedelta(days=90)}
        if len(state['pending'])>1500:
            state['pending_overflow']=state.get('pending_overflow',0)+len(state['pending'])-1500
            state['pending']=dict(list(state['pending'].items())[-1500:])
        urgent=[]
        if notify and cfg['urgent_hours']:
            for x in state['items'].values():
                if x['source'] not in cfg['sources'] or x.get('last_seen')!=now.isoformat() or not x.get('matched_by') or not x.get('deadline') or state['sources'].get(x['source'],{}).get('state')!='ok':continue
                try:
                    end=parse_time(x['deadline'])
                    if not isinstance(end,datetime):end=datetime.combine(end,datetime.min.time(),CST)+timedelta(days=1)
                    key=x['deadline']
                    if timedelta(0)<=end-now<=timedelta(hours=cfg['urgent_hours']) and x.get('urgent_key')!=key:
                        urgent.append({**x,'change':'deadline'});x['urgent_key']=key
                except ValueError:pass
        local=now.astimezone(CST);today=local.date().isoformat()
        daily_due=local.strftime('%H:%M')>=cfg['daily_time'] and state.get('last_daily')!=today
        events=list(state['pending'].values()) if notify and daily_due else []
        events+=urgent
        if events:
            # One catch-up report, never a burst of old daily emails.
            unique={x['id']:x for x in events}
            message=dict(id=uuid.uuid4().hex,created_at=now.isoformat(),read=False,items=[{'id':x['id'],'change':x.get('change')} for x in unique.values()],
                         text=report_text(unique.values(),state['sources']),delivery={'status':'pending'},subscription_version=state.get('subscription_version'))
            if state.get('pending_overflow'):
                message['text']+='\n待汇总变化曾超过 1500 项，另有 '+str(state['pending_overflow'])+' 项未保留，请缩小订阅范围并核对原站。'
            state['messages'].append(message);state['messages']=state['messages'][-60:]
            if daily_due:state['pending']={};state['last_daily']=today;state['pending_overflow']=0
            else:
                for x in urgent:state['pending'].pop(x['id'],None)
            # Durable app notification first; email is separately tracked.
            store.save(state)
            try:_deliver(state,message,store,sender)
            except Exception as exc:
                message['delivery'].update(status='unknown',**record(service='digest',stage='background',exc=exc,outcome='unknown'))
        elif notify and daily_due:state['last_daily']=today
        state['last_cycle']=now.isoformat();store.save(state)
        return {'state':'refreshed','baseline_sources':baseline,'item_count':len(state['items']),
                'source_health':state['sources'],'messages':len(state['messages']),'notification_created':bool(events)}

def _refresh_native(*,notify,store,collector,now,sender):
    """Temporary IO orchestration. Rust owns reduction, filtering and app delivery.

    Keep the shared cycle lock across collection and saves; failure classification,
    collector HTTP/DOM and optional email delivery still use existing adapters.
    """
    with store.cycle():
        state=store.read();cfg=state['settings'];notify=notify and cfg['enabled']
        if notify and state.get('last_cycle') and now-datetime.fromisoformat(state['last_cycle'])<timedelta(minutes=cfg['interval_minutes']):
            return {'state':'not_due','last_cycle':state['last_cycle']}
        if notify:
            for message in state['messages']:
                if message.get('delivery',{}).get('status') in {'pending','prepared','sending'}:
                    # Switching to app-only must not rewrite an older SMTP
                    # attempt as successfully delivered inside the app.
                    if not cfg['email_enabled'] and (message['delivery'].get('draft_id') or message['delivery']['status'] in {'prepared','sending'}):
                        continue
                    try:_deliver(state,message,store,sender)
                    except Exception as exc:
                        message['delivery'].update(status='unknown',**record(service='digest',stage='background',exc=exc,outcome='unknown'))
        baseline=[]
        for source in cfg['sources']:
            previous=state['sources'].get(source,{})
            if previous.get('retry_at') and now<datetime.fromisoformat(previous['retry_at']):continue
            try:
                response=collector(source,cfg)
                result=native.call('apply_source',state=state,source=source,response=response,now=now.isoformat())
                state=result['state']
                if result['baseline']:baseline.append(source)
            except Exception as exc:
                failures=previous.get('failures',0)+1
                incident=record(service='digest',stage='background',exc=exc,outcome='read_failed')
                from ..recovery import classify
                error_code,_=classify(exc)
                state['sources'][source]={**previous,'state':'failed','failures':failures,'error_id':incident['error_id'],
                    'error_code':error_code,'next_action':'在账号页检查该平台登录' if error_code in {'authentication_required','identity_required','manual_verification_required'} else '检查来源状态，等待冷却后重试',
                    'retry_at':(now+timedelta(minutes=min(1440,cfg['interval_minutes']*2**min(failures-1,5)))).isoformat()}
                if cfg['enabled'] and previous.get('state')!='failed':
                    state['pending']['error:'+source]=dict(id='error:'+source,title=source+' 暂时读取失败',source=source,
                        source_url='',summary='本次未检查成功，不能据此判断没有新消息。错误编号：'+incident['error_id'],change='source_failed')
        result=native.call('finish_cycle',state=state,now=now.isoformat(),notify=notify)
        state=result['state'];message_id=result['message_id']
        # App-only delivery is already final in the Rust result. Freeze the exact
        # report before any optional SMTP adapter and preserve unknown attempts.
        store.save(state)
        if message_id and cfg['email_enabled']:
            message=next(x for x in state['messages'] if x['id']==message_id)
            try:_deliver(state,message,store,sender)
            except Exception as exc:
                message['delivery'].update(status='unknown',**record(service='digest',stage='background',exc=exc,outcome='unknown'))
            store.save(state)
        return {'state':'refreshed','baseline_sources':baseline,'item_count':len(state['items']),
                'source_health':state['sources'],'messages':len(state['messages']),'notification_created':bool(message_id)}

def overview(store=None,limit=100):
    if native.enabled():return _native_store('overview',store or Store(),limit=limit)
    state=(store or Store()).read();cfg=state['settings']
    items=[dict(x,matched_by=matches(x,cfg)) for x in state['items'].values() if x['source'] in cfg['sources']]
    items=[x for x in items if x['matched_by']]
    items.sort(key=lambda x:(x.get('deadline') or '9999',x.get('start') or '9999',x['title']))
    return dict(settings=cfg,sources=catalog(),source_health=state['sources'],items=deduplicate(items)[:limit],
        total_matched=len(items),truncated=len(items)>limit or state.get('storage_truncated',False),
        pending_overflow=state.get('pending_overflow',0),
        messages=[{k:v for k,v in x.items() if k!='items'} for x in reversed(state['messages'])],
        last_cycle=state.get('last_cycle'),preview_only=not cfg['enabled'],
        note='未提供截止时间的条目不会猜测日期。列表未出现不代表取消或已完成。')

def mark_read(message_id,store=None):
    store=store or Store()
    if native.enabled():return _native_store('mark_read',store,message_id=message_id)
    with store.cycle():
        state=store.read();match=next((x for x in state['messages'] if x['id']==message_id),None)
        if not match:raise ValueError('简报不存在。')
        match['read']=True;store.save(state)
    return {'read':True}

def preview_report(store=None):
    """User-requested app inbox sample; does not send mail or consume daily changes."""
    store=store or Store()
    if native.enabled():return _native_store('preview_report',store)
    with store.cycle():
        state=store.read();view=overview(store)
        text='【试阅简报】当前采集快照，不表示这些消息刚刚发布。\n\n'+report_text(view['items'],state['sources'])
        if not view['items']:text+='\n尚无匹配内容，请先采集或检查来源状态。'
        key=hashlib.sha256(text.encode()).hexdigest()
        old=next((x for x in state['messages'] if x.get('preview_key')==key),None)
        if old:return {'state':'existing','message_id':old['id'],'text':old['text'],'delivery':old['delivery']}
        message=dict(id=uuid.uuid4().hex,created_at=utcnow().isoformat(),read=False,items=[],text=text,
                     preview_key=key,delivery={'status':'app_only'},kind='preview')
        state['messages'].append(message);state['messages']=state['messages'][-60:];store.save(state)
    return {'state':'created','message_id':message['id'],'text':text,'delivery':message['delivery']}
