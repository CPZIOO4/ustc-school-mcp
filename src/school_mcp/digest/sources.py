"""Bounded read adapters. Failure/partial coverage is never a successful empty list."""
import re
from . import public

PRIVATE = {'teach':'教务处通知', 'portal':'个人待办', 'scholarships':'奖学金申请通知',
           'library':'借阅到期', 'young':'第二课堂活动', 'bb':'所选 BB 课程作业'}

def catalog():
    return [dict(id=k,label=v[0],public=True,url=v[1]) for k,v in public.SOURCES.items()] + [
        dict(id=k,label=v,public=k=='teach') for k,v in PRIVATE.items()]

def collect(source, settings):
    max_pages=settings.get('max_pages',3)
    if source in public.SOURCES:
        rows=[];page=1;next_page=None;unsupported=0
        for _ in range(max_pages):
            r=public.listing(source,page);rows+=r['items'];next_page=r.get('next_page')
            unsupported+=r.get('unsupported_links',0)
            if not next_page:break
            page=next_page
        # Detail is bounded; refresh details of matched recent entries every time,
        # so corrected dates/cancellations can produce a change event.
        detail_errors=0
        for n,x in enumerate(rows):
            if n>=settings.get('detail_limit',10):break
            try:
                full=public.detail(source,x['source_url'])
                rows[n]=x|{k:v for k,v in full.items() if v not in (None,'',{})}
            except Exception:
                detail_errors+=1
        excluded=sum(x.get('quality')=='insufficient_information' for x in rows)
        return dict(items=[x for x in rows if x.get('quality')!='insufficient_information'],complete=next_page is None and not unsupported,next_page=next_page,
                    unsupported_links=unsupported,
                    excluded_low_quality=excluded,
                    scope='最多读取所选页数；详情仅前若干条，未覆盖部分可继续按页查询。',detail_failures=detail_errors)
    if source=='teach':
        from ..teach.client import TeachClient
        c=TeachClient();rows=[];more=None
        for page in range(1,max_pages+1):
            r=c.list_notices(page=page)
            if r.get('access')!='public':raise ValueError('通知需要重新核实访问权限。')
            for x in r['records']:
                rows.append(public.item(source,x['source_url'],x['title'],x['source_url'],published_at=x.get('published_at')))
            more=r.get('next_page_url')
            if not more:break
        for x in rows[:settings.get('detail_limit',10)]:
            try:
                d=c.read_article(x['source_url']);x.update(summary=d.get('text','')[:12000]);x.update(public._fields(d.get('text','')))
            except Exception:
                x['detail_unavailable']=True
        return dict(items=rows,complete=not more,scope='公开通知已扫描页；不保证覆盖全部历史通知。')
    if source=='portal':
        from ..portal.client import progress
        rows=[];next_page=None
        for page in range(1,max_pages+1):
            r=progress(page=page,limit=50)
            for x in r['items']:
                rows.append(public.item(source,x.get('id') or str(x),x.get('title') or x.get('name') or x.get('flow_name') or '待办',r['source_url'],
                    summary=' · '.join(str(x[k]) for k in ['app_name','status_name','node_name'] if x.get(k)),needs_action=True))
            next_page=r.get('next_page')
            if not next_page:break
        return dict(items=rows,complete=not next_page,scope='仅一网通办待办，不包含其他平台。')
    if source=='scholarships':
        from ..student.client import scholarships
        r=scholarships();rows=[]
        for x in r['items']:
            key=next((d['reference'] for d in x.get('details',[]) if d.get('reference')),x['name'])
            content='\n'.join(x.get('fields',[]))
            rows.append(public.item(source,key,x['name'],r['source_url'],summary=content,**public._fields(content)))
        return dict(items=rows,complete=True,scope='当前开放奖学金列表；资格与截止时间须查看原文。')
    if source=='library':
        from ..library.client import LibraryClient
        r=LibraryClient().loans(include_identifiers=True)
        rows=[]
        for x in r['records']:
            name=x.get('title') or x.get('title_author')
            rows.append(public.item(source,x.get('barcode') or name,name,'https://lib.ustc.edu.cn/',
                deadline=public._dated(x.get('due_date')),summary='图书借阅应还日期',time_evidence={'deadline':x.get('due_date')}))
        return dict(items=rows,complete=True,scope=r.get('scope'))
    if source=='young':
        from ..young.registration import list_projects, school_time
        rows=[];more=None
        for page in range(1,max_pages+1):
            r=list_projects(page)
            for x in r['projects']:
                key=x.get('id') or x.get('item_id');title=x.get('name') or x.get('itemName')
                if not key or not title:raise ValueError('第二课堂条目结构变化。')
                record=public.item(source,key,title,'https://young.ustc.edu.cn/login/sc-wisdom-group-learning/',
                    summary='当前报名列表可见；资格与场次请查看详情。')
                for field,raw in [('deadline','applyEt'),('start','st'),('end','et')]:
                    try:
                        record[field]=school_time(x.get(raw)).isoformat();record['time_evidence'][field]=str(x[raw])
                    except Exception:pass
                rows.append(record)
            more=r.get('next_page')
            if not more:break
        return dict(items=rows,complete=not more,scope='本人可见报名中项目；可见不代表有资格。')
    if source=='bb':
        from ..bb.assignments import catalog as assignments
        rows=[];complete=True
        for course in settings.get('bb_courses',[]):
            r=assignments(course,max_pages=5,limit=50);complete=complete and not r['truncated']
            for x in r['assignments']:
                rows.append(public.item(source,course+':'+x['content_id'],x['title'],
                    'https://www.bb.ustc.edu.cn'+x['entry_path'],summary=x['description'],**public._fields(x['description'])))
        if not settings.get('bb_courses'):raise ValueError('请先选择要订阅的 BB 课程。')
        return dict(items=rows,complete=complete,scope='仅用户选择的课程；未打开提交页面，未核实是否已交。')
    raise ValueError('信息源未适配。')
