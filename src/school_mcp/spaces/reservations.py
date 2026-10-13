"""Fixed study-space reservation/cancellation plans with live prechecks and readback."""
from datetime import datetime, timedelta, timezone
import re
from ..bb.identity import load_credentials
from ..campus.session import browser_session
from ..network import limiter
from ..service_errors import ServiceError
from ..workflow_store import PlanStore,digest
from .client import connected,request

CST=timezone(timedelta(hours=8))


def store():return PlanStore('spaces',digest(load_credentials()['username']))


def timestamp(value):
    if isinstance(value,(int,float)):
        return datetime.fromtimestamp(value/1000 if value>1e11 else value,CST)
    if isinstance(value,str):
        parsed=datetime.fromisoformat(value.replace('/','-'))
        return parsed.astimezone(CST) if parsed.tzinfo else parsed.replace(tzinfo=CST)
    raise ServiceError('预约时间格式变化，不能推测。',code='unavailable')


def raw_mine(s,date,page=1,limit=50):
    data=request(s,'/ic-web/reserve/resvInfo',{'beginDate':date,'endDate':date,'needStatus':0,'page':page,'pageNum':limit,'orderKey':'gmt_create','orderModel':'desc'})
    if not isinstance(data.get('data'),list):raise ServiceError('本人预约列表结构变化。',code='unavailable')
    return data


def public_row(x):
    return {k:x.get(k) for k in ('uuid','resvId','testName','resvBeginTime','resvEndTime','resvStatus')} | {
        'resources':[{k:d.get(k) for k in ('devId','devName','roomName','labName')} for d in x.get('resvDevInfoList',[])]}


def mine(date,page=1,limit=20):
    try:datetime.strptime(date,'%Y-%m-%d')
    except (ValueError,TypeError):raise ServiceError('日期须为 YYYY-MM-DD。',code='invalid_arguments') from None
    if not 1<=page<=100 or not 1<=limit<=50:raise ServiceError('分页范围无效。',code='invalid_arguments')
    with browser_session('spaces') as s:
        connected(s);data=raw_mine(s,date,page,limit);total=int(data.get('count') or 0)
        return {'date':date,'items':[public_row(x) for x in data['data']],'total':total,'next_page':page+1 if total>page*limit else None,'untrusted_content':True}


def all_mine(s,date):
    rows=[]
    for page in range(1,6):
        data=raw_mine(s,date,page);rows.extend(data['data'])
        if len(rows)>=int(data.get('count') or 0):return rows
    raise ServiceError('预约记录过多，本次没有读全，已停止变更。',code='unavailable')


def room(s,kind_id,dev_id,date):
    for page in range(1,6):
        data=request(s,'/ic-web/reserve',{'sysKind':1,'resvDates':date.replace('-',''),'page':page,'pageSize':50,'kindIds':kind_id,'labId':''})
        found=next((x for x in data['data'] if x['devId']==dev_id),None)
        if found:return found
        if page*50>=int(data.get('count') or 0):break
    raise ServiceError('资源不在所选日期和分类中，请重新查询。',code='invalid_arguments')


def validate_room(resource,begin,end,people,now=None):
    now=now or datetime.now(CST)
    if begin.date()!=end.date() or not now<begin<end:
        raise ServiceError('仅支持未来同一天的预约，结束须晚于开始。',code='invalid_arguments')
    if resource.get('onlyView') or resource.get('devStatus')!=0 or not resource.get('minUser',1)<=people<=resource.get('maxUser',1):
        raise ServiceError('资源不可预约或人数不符合限制。',code='invalid_arguments')
    rules=resource.get('resvRule') or {};duration=(end-begin).total_seconds()/60;advance=(begin-now).total_seconds()/60
    for value,maximum,minimum in [(duration,rules.get('maxResvTime'),rules.get('minResvTime')),(advance,rules.get('earliestResvTime'),rules.get('latestResvTime'))]:
        if maximum and value>maximum or minimum and value<minimum:
            raise ServiceError('时长或提前预约时间不符合当前规则。',code='invalid_arguments')
    interval=rules.get('timeInterval') or 1
    if begin.minute%interval or end.minute%interval:
        raise ServiceError('开始和结束时间不符合平台时间间隔。',code='invalid_arguments')
    windows=resource.get('openTimes') or []
    if not any(w['openStartTime']<=begin.strftime('%H:%M') and end.strftime('%H:%M')<=w['openEndTime'] for w in windows):
        raise ServiceError('所选时间不在开放时段内。',code='invalid_arguments')
    for r in resource.get('resvInfo',[]):
        a=r.get('startTime',r.get('resvBeginTime'));b=r.get('endTime',r.get('resvEndTime'))
        if isinstance(a,str) and re.fullmatch(r'\d{2}:\d{2}(?::\d{2})?',a):a=begin.strftime('%Y-%m-%d')+' '+a
        if isinstance(b,str) and re.fullmatch(r'\d{2}:\d{2}(?::\d{2})?',b):b=begin.strftime('%Y-%m-%d')+' '+b
        if begin<timestamp(b) and end>timestamp(a):raise ServiceError('该时段已被占用。',code='invalid_arguments')


def prepare(kind_id,dev_id,begin,end,title,participants=None):
    try:
        start=datetime.strptime(begin,'%Y-%m-%d %H:%M').replace(tzinfo=CST);finish=datetime.strptime(end,'%Y-%m-%d %H:%M').replace(tzinfo=CST)
    except (ValueError,TypeError):raise ServiceError('时间须为 YYYY-MM-DD HH:MM，使用北京时间。',code='invalid_arguments') from None
    if not isinstance(title,str) or not 1<=len(title.strip())<=100 or type(kind_id) is not int or type(dev_id) is not int:
        raise ServiceError('请提供真实用途和资源编号。',code='invalid_arguments')
    participants=participants or []
    if len(participants)>20 or any(not isinstance(x,str) or not re.fullmatch('[A-Za-z0-9_.-]{1,50}',x) for x in participants):
        raise ServiceError('参与者须为明确的学校账号，最多 20 人。',code='invalid_arguments')
    with browser_session('spaces') as s:
        user=connected(s);members=[user['accNo']];accounts=[s.account]
        for account in dict.fromkeys(participants):
            if account.casefold()==s.account.casefold():continue
            data=request(s,'/ic-web/account/getMembers',{'key':account,'page':1,'pageNum':10})['data']
            match=[x for x in data if str(x.get('logonName','')).casefold()==account.casefold()]
            if len(match)!=1 or match[0].get('status')==2 or match[0].get('localstatus')==2:
                raise ServiceError('未能唯一核对某位参与者，请确认账号。',code='invalid_arguments')
            members.append(match[0]['accNo']);accounts.append(account)
        resource=room(s,kind_id,dev_id,start.strftime('%Y-%m-%d'));validate_room(resource,start,finish,len(members))
        existing=all_mine(s,start.strftime('%Y-%m-%d'))
        content={'operation':'reserve','kind_id':kind_id,'dev_id':dev_id,'begin':begin,'end':end,'title':title.strip(),
                 'members':members,'accounts':accounts,'appAccNo':user['accNo']}
        snapshot={'before_ids':[x['uuid'] for x in existing], 'resource':{k:resource.get(k) for k in ('devName','labName','minUser','maxUser','resvRule')}}
        preview={'campus':'西区图书馆','resource':resource['devName'],'floor':resource.get('labName'),'begin':begin,'end':end,'purpose':title,'participants':accounts}
        return store().create(digest([dev_id,begin,end]),content,snapshot,{},preview)


def prepare_cancel(date,reservation_id):
    try:datetime.strptime(date,'%Y-%m-%d')
    except (ValueError,TypeError):raise ServiceError('日期须为 YYYY-MM-DD。',code='invalid_arguments') from None
    if not isinstance(reservation_id,str) or not re.fullmatch('[a-fA-F0-9-]{20,40}',reservation_id):
        raise ServiceError('请使用本人预约列表返回的 uuid。',code='invalid_arguments')
    with browser_session('spaces') as s:
        user=connected(s);rows=all_mine(s,date);item=next((x for x in rows if x.get('uuid')==reservation_id),None)
        if not item or item.get('appAccNo')!=user['accNo'] or int(item.get('resvStatus',0))&(4|128):
            raise ServiceError('这不是本人当前可取消的预约。',code='invalid_arguments')
        return store().create(reservation_id,{'operation':'cancel','date':date,'uuid':reservation_id,'appAccNo':user['accNo']},
                              {'reservation':public_row(item)}, {}, {'operation':'cancel','reservation':public_row(item)})


def post(s,path,payload):
    if path not in {'/ic-web/reserve','/ic-web/reserve/delete'}:raise ValueError('unknown operation')
    limiter.acquire('spaces')
    r=s.context.request.post(s.base+path,data=payload,timeout=30000,max_redirects=0)
    limiter.response('spaces',r.status,r.headers)
    if r.status!=200 or len(r.body())>100000:raise ServiceError('预约请求结果未确认。',code='unavailable')
    return r.json()


def execute(plan_id,content_sha256,authorized=False):
    if authorized is not True:raise ServiceError('需要对该资源、时间、用途和参与者的明确授权。',code='authorization_required')
    saved=store();item=saved.get(plan_id);c=item['plan']['content']
    if item['state']!='ready':return saved.view(plan_id)
    with browser_session('spaces') as s:
        user=connected(s)
        if user['accNo']!=c['appAccNo']:raise ServiceError('账号已变化。',code='identity_mismatch')
        if c['operation']=='reserve':
            begin=timestamp(c['begin']);end=timestamp(c['end']);resource=room(s,c['kind_id'],c['dev_id'],begin.strftime('%Y-%m-%d'))
            validate_room(resource,begin,end,len(c['members']))
            config={x['sysKey']:x['sysValue'] for x in request(s,'/ic-web/sysConfig/public')['data']}
            if config.get('resvCode','0')!='0':
                return {**saved.view(plan_id),'requires_user_action':True,'state':'manual_verification_required',
                        'next_step':'学校当前要求预约验证码，请在官方预约界面完成验证；本计划尚未提交。'}
            payload={'sysKind':1,'appAccNo':user['accNo'],'memberKind':2 if len(c['members'])>1 else 1,
                'resvBeginTime':begin.strftime('%Y-%m-%d %H:%M:00'),'resvEndTime':end.strftime('%Y-%m-%d %H:%M:00'),
                'testName':c['title'],'resvKind':2,'resvProperty':0,'appUrl':'','resvMember':c['members'],'resvDev':[c['dev_id']],
                'memo':'','captcha':'','addServices':[]}
            path='/ic-web/reserve'
        else:
            rows=all_mine(s,c['date']);original=next((x for x in rows if x.get('uuid')==c['uuid']),None)
            if not original or original.get('appAccNo')!=user['accNo'] or int(original.get('resvStatus',0))&(4|128):
                raise ServiceError('原预约已变化，不能按旧预览取消。',code='invalid_arguments')
            path='/ic-web/reserve/delete';payload={'uuid':c['uuid']}
        if not saved.claim(plan_id,content_sha256):return saved.view(plan_id)
        try:
            response=post(s,path,payload)
            if response.get('code')!=0:
                saved.finish(plan_id,'stopped',{'accepted':False,'message':str(response.get('message','平台未接受该操作。'))[:300],'untrusted_content':True})
                return saved.view(plan_id)
            saved.checkpoint(plan_id,{'accepted':True})
            return verify_with(s,saved,plan_id)
        except Exception:
            previous=saved.get(plan_id).get('result') or {}
            saved.finish(plan_id,'uncertain',{**previous,'note':'提交或取消结果不完整；只查询原记录，不重发。'})
            return saved.view(plan_id)


def verify_with(s,saved,plan_id):
    item=saved.get(plan_id);c=item['plan']['content'];date=c.get('date') or c['begin'].split()[0];rows=all_mine(s,date)
    if c['operation']=='cancel':
        verified=not any(x.get('uuid')==c['uuid'] for x in rows)
        result={'cancelled_from_current_list':verified,'uuid':c['uuid'],'date':date}
    else:
        candidates=[x for x in rows if x.get('uuid') not in item['plan']['snapshot']['before_ids'] and x.get('appAccNo')==c['appAccNo']
            and x.get('testName')==c['title'] and any(d.get('devId')==c['dev_id'] for d in x.get('resvDevInfoList',[]))
            and timestamp(x['resvBeginTime'])==timestamp(c['begin']) and timestamp(x['resvEndTime'])==timestamp(c['end'])]
        verified=len(candidates)==1
        result={'reservation':public_row(candidates[0]) if verified else None,'reservation_visible':verified,
                'note':'记录已出现，具体审核/生效状态仍以 resvStatus 和官方页面为准。'}
    saved.finish(plan_id,'verified' if verified else 'uncertain',{**result,'verified':verified,'untrusted_content':True})
    return saved.view(plan_id)


def status(plan_id,verify=True):
    saved=store();item=saved.get(plan_id)
    if not verify or item['state'] not in {'executing','uncertain'}:return saved.view(plan_id)
    with browser_session('spaces') as s:
        connected(s);return verify_with(s,saved,plan_id)
