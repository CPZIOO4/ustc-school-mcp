"""RFC 5545 calendar snapshots with stable identities and explicit time evidence."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
import uuid
from ..mail.config import local_dir

CST=timezone(timedelta(hours=8))

def parse_time(value):
    if not isinstance(value,str):
        raise ValueError('事项缺少已确认的时间。')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
        return date.fromisoformat(value)
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:
        raise ValueError('时间须包含时区。')
    return result

def escaped(value):
    return str(value or '').replace('\\','\\\\').replace('\r\n','\n').replace('\r','\n').replace('\n','\\n').replace(';','\\;').replace(',','\\,')

def folded(line):
    lines=[];current='';size=0
    for char in line:
        n=len(char.encode('utf-8'))
        if size+n>75:
            lines.append(current);current=' ';size=1
        current+=char;size+=n
    return '\r\n'.join(lines+[current])

def render(events, generated_at=None):
    stamp=(generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    lines=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//School MCP//Campus Calendar//ZH','CALSCALE:GREGORIAN','METHOD:PUBLISH']
    skipped=[];seen=set();count=0
    for x in events:
        try:
            start=parse_time(x.get('start') or x.get('deadline'))
            end=parse_time(x['end']) if x.get('end') else None
            if end and (type(end)!=type(start) or end<=start):
                raise ValueError('结束时间须晚于开始时间且精度一致。')
            identifier=x['id']
            if identifier in seen:
                continue
            seen.add(identifier)
        except (ValueError,KeyError,TypeError):
            skipped.append({'id':x.get('id'),'reason':'missing_or_ambiguous_time'});continue
        uid=hashlib.sha256(str(identifier).encode()).hexdigest()+'@school-mcp.local'
        lines+=['BEGIN:VEVENT','UID:'+uid,'DTSTAMP:'+stamp,'SEQUENCE:'+str(max(0,int(x.get('revision',0))))]
        if isinstance(start,datetime):
            lines.append('DTSTART:'+start.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
            if end:
                lines.append('DTEND:'+end.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        else:
            lines.extend(['DTSTART;VALUE=DATE:'+start.strftime('%Y%m%d'),
                          'DTEND;VALUE=DATE:'+(end or start+timedelta(days=1)).strftime('%Y%m%d')])
        for key,field in [('SUMMARY','title'),('LOCATION','location')]:
            lines.append(key+':'+escaped(x.get(field,'')))
        lines.append('DESCRIPTION:'+escaped((x.get('summary') or '')[:1500]+'\n来源：'+x.get('source_url','')))
        if x.get('status')=='cancelled':
            lines.append('STATUS:CANCELLED')
        lines.append('END:VEVENT');count+=1
    lines.append('END:VCALENDAR')
    return {'content':'\r\n'.join(folded(x) for x in lines)+'\r\n','count':count,'skipped':skipped,
            'note':'日历文件为当前快照；稳定 UID 与版本号供支持更新的客户端匹配。导入后不会自动同步。'}

def export(events):
    from .store import Store
    store=Store()
    with store.cycle():
        state=store.read();versions=state.setdefault('calendar_versions',{});versioned=[]
        for event in events:
            identifier=event['id']
            content=json.dumps({k:event.get(k) for k in ['title','start','end','deadline','location','status','summary','source_url']},sort_keys=True,ensure_ascii=False)
            fingerprint=hashlib.sha256(content.encode()).hexdigest()
            old=versions.get(identifier,{})
            revision=max(event.get('revision',0),old.get('revision',0)+(1 if old and old['fingerprint']!=fingerprint else 0))
            versions[identifier]={'fingerprint':fingerprint,'revision':revision}
            versioned.append(dict(event,revision=revision))
        if len(versions)>20000:raise ValueError('日历版本记录已达上限，请先管理本机信息中心数据。')
        result=render(versioned);store.save(state)
    folder=local_dir()/'calendar-exports';folder.mkdir(parents=True,exist_ok=True)
    path=folder/(uuid.uuid4().hex+'.ics');raw=result.pop('content').encode('utf-8');path.write_bytes(raw)
    return {**result,'path':str(path.resolve()),'filename':'校园日历.ics','size_bytes':len(raw),
            'sha256':hashlib.sha256(raw).hexdigest()}

def timetable(semester_id=0, client=None):
    from ..jw.client import JWClient
    client=client or JWClient()
    context,data=client.course_data(semester_id)
    table=client.timetable(context['semester']['id'])
    dates=data.get('oddWeekIndex2dayOfWeek2Date',{})
    events=[];skipped=[]
    for n,row in enumerate(table['activities']):
        if row.get('self_defined_dates'):
            skipped.append({'row':n,'reason':'custom_date_requires_review'});continue
        start,end=row.get('start'),row.get('end')
        if not all(isinstance(t,str) and re.fullmatch(r'\d{2}:\d{2}(?::\d{2})?',t) for t in (start,end)):
            skipped.append({'row':n,'reason':'clock_time_unknown'});continue
        for week in row.get('weeks') or []:
            day=dates.get(str(week),{}).get(str(row.get('weekday')))
            if not day:
                skipped.append({'row':n,'week':week,'reason':'official_date_missing'});continue
            try:
                date.fromisoformat(day)
                a=datetime.fromisoformat(day+'T'+start).replace(tzinfo=CST)
                b=datetime.fromisoformat(day+'T'+end).replace(tzinfo=CST)
                if b<=a:raise ValueError()
            except (ValueError,TypeError):
                skipped.append({'row':n,'week':week,'reason':'invalid_official_time'});continue
            # Week/period identity remains stable after a time or room change.
            key=':'.join(map(str,[context['semester']['id'],row.get('lesson_id'),row.get('group'),week,row.get('weekday'),row.get('start_period'),row.get('end_period')]))
            events.append(dict(id='jw:'+key,title=row.get('course_name') or '课程',start=a.isoformat(),end=b.isoformat(),
                location=' '.join(str(row.get(k) or '') for k in ['campus','building','room']).strip(),
                source='jw',source_url='https://jw.ustc.edu.cn/for-std/course-table',
                time_evidence={'date':'JW week/date mapping','clock':'JW startDate/endDate'},summary='教务课表；教学分组请以本人实际安排为准。'))
    return {'events':events,'skipped':skipped,'semester':context['semester'],'complete':not skipped}
