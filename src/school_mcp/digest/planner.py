"""Propose subscription parameters only; never save, enable or send."""
import json
import re
from .engine import settings
from .store import defaults
from .sources import catalog

PRESETS = {
    'lectures': ('学术讲座', ['math_lectures','cs_lectures','physics_lectures']),
    'careers': ('实习招聘', ['jobs','internships','research_jobs','job_events']),
    'lectures_careers': ('学术讲座＋实习招聘', ['math_lectures','cs_lectures','physics_lectures','jobs','internships','research_jobs','job_events']),
    'coursework': ('课程作业', ['bb']),
}
ALLOWED = {'sources','keywords','exclude','locations','daily_time','interval_minutes','urgent_hours'}

def presets():
    return [{'id':key,'label':v[0],'sources':v[1]} for key,v in PRESETS.items()]

def propose(request='', preset=None, base=None, model=None):
    if not isinstance(request,str) or len(request)>1000:raise ValueError('订阅需求最多 1000 字。')
    if preset is not None and preset not in PRESETS:raise ValueError('请选择已有订阅预设。')
    if not request.strip() and preset is None:raise ValueError('选择预设或描述想关注的内容。')
    proposal={**defaults(),**(base or {})};proposal['enabled']=False
    if preset:proposal['sources']=list(PRESETS[preset][1])
    warnings=[];mode='preset'
    if request.strip():
        # Model may propose only filtering/time fields, tied to literal user evidence.
        # Credentials, delivery recipients and activation are never model fields.
        if model is None:
            raise ValueError('一句话配置需要已连接的模型；也可以直接选择预设。')
        answer=model.complete([
            {'role':'system','content':'把用户的校园订阅需求转换成配置补丁。只返回 JSON 对象 {"changes": {字段: {"value": 值, "evidence": "用户原话中的连续片段"}}}。允许字段 sources(来源id列表)、keywords(任一匹配词列表)、exclude(排除词列表)、locations(地点列表)、daily_time(北京时间HH:MM)、interval_minutes(整数)、urgent_hours(整数)。只修改用户明确提出的条件，不补充用户没说的关键词或地点。不设收件人、不启用、不执行。类别名称用于选择来源，不要把“学术讲座”“实习招聘”当成关键词过滤。用户使用预设类别且未进一步限制时，sources 应包含该预设全部来源：'+json.dumps(presets(),ensure_ascii=False)+'。来源目录：'+json.dumps(catalog(),ensure_ascii=False)},
            {'role':'user','content':request}],[])
        try:
            changes=json.loads(answer.get('content',''))['changes']
            if not isinstance(changes,dict) or not changes or set(changes)-ALLOWED:raise ValueError()
            for key,entry in changes.items():
                if not isinstance(entry,dict) or set(entry)!={'value','evidence'}:raise ValueError()
                evidence=entry['evidence']
                if not isinstance(evidence,str) or not evidence.strip() or evidence not in request:raise ValueError()
                if key in {'keywords','exclude','locations'}:
                    if not isinstance(entry['value'],list) or any(not isinstance(x,str) or x.casefold() not in request.casefold() for x in entry['value']):raise ValueError()
                proposal[key]=entry['value']
            mode='model_proposal'
        except (ValueError,TypeError,KeyError):
            raise ValueError('模型未返回可核对的配置，请换一种描述或使用预设；原订阅未改变。') from None
        # "只在应用" is an explicit delivery preference, not model discretion.
        if re.search(r'不(?:发|用).*邮件|只.*(?:应用|[Aa]gent)',request):
            proposal.update(email_enabled=False,email='')
    missing=[]
    if 'bb' in proposal['sources'] and not proposal['bb_courses']:
        missing.append('在 BB 课程选择中勾选实际课程后再保存。')
        check={**proposal,'sources':[x for x in proposal['sources'] if x!='bb'] or ['teach']}
        settings(check)
    else:proposal=settings(proposal)
    labels={x['id']:x['label'] for x in catalog()}
    preview={'来源':[labels[x] for x in proposal['sources']], '关注词':proposal['keywords'] or ['全部'],
        '排除词':proposal['exclude'],'地点':proposal['locations'] or ['全部'],
        '日报时间':proposal['daily_time']+'（北京时间）','检查间隔':str(proposal['interval_minutes'])+' 分钟',
        '提前提醒':str(proposal['urgent_hours'])+' 小时','接收位置':'应用内'+('及已配置邮箱' if proposal['email_enabled'] else ''),
        'BB课程':proposal['bb_courses']}
    return {'state':'needs_input' if missing else 'preview','settings':proposal,'preview':preview,
        'missing':missing,'mode':mode,'warnings':warnings,'saved':False,'enabled':False,
        'note':'仅生成候选配置；核对后应用到表单，再保存或开启。没有改变原订阅。'}
