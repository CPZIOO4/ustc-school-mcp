"""Publish a new review once. Existing reviews are never silently overwritten."""
from __future__ import annotations

import html
import re
from urllib.parse import urlsplit, urljoin

import httpx
from bs4 import BeautifulSoup
from ..network import http_client, bounded_request
from ..workflow_store import PlanStore, digest, require, WorkflowError
from .session import load_session
from .client import BASE, bounded, ICourseError


RATINGS = ('difficulty','homework','grading','gain','rate')


def form_snapshot(source, course_id):
    soup=BeautifulSoup(source,'html.parser')
    form=soup.select_one('form#review-form')
    require(form is not None and form.get('action')==f'/course/{course_id}/review/' and form.get('method','').lower()=='post', '点评表单不可用，可能需要登录或页面已变化。')
    for name in ('csrf_token','term','content',*RATINGS,'is_anonymous','only_visible_to_student'):
        require(form.select_one(f'[name="{name}"]') is not None, '点评字段已变化。')
    identities={m[1] for a in soup.select('a[href]') if a.get_text(strip=True)=='个人主页' and (m:=re.fullmatch(r'/user/(\d+)',a['href']))}
    require(len(identities)==1, '未能确认当前评课账号。')
    heading=soup.select_one('.inline-h3')
    require(heading is not None, '无法判断新点评或编辑状态。')
    terms=[{'id':o['value'],'label':o.get_text(' ',strip=True)} for o in form.select('select[name=term] option[value]') if o['value'].isdigit()]
    selected=form.select_one('select[name=term] option[selected]')
    values={}
    for name in RATINGS:
        node=form.select_one(f'[name="{name}"]:checked') if name!='rate' else form.select_one('[name=rate]')
        value=node.get('value') if node else None
        try:values[name]=int(float(value)*2) if name=='rate' and value else int(value) if value else None
        except (TypeError,ValueError):raise WorkflowError('评分表单值异常。') from None
        if not values[name]:values[name]=None
    content=form.select_one('textarea[name=content]').get_text()
    require(bool(form.select_one('[name=csrf_token]').get('value')), '缺少表单校验令牌，停止发布。')
    return dict(user_id=identities.pop(), terms=terms, course_title=heading.get_text(' ',strip=True),
                existing=heading.get_text(' ',strip=True).startswith('编辑'), csrf=form.select_one('[name=csrf_token]').get('value',''),
                values=dict(term=selected.get('value') if selected else None,content=content,**values,
                            is_anonymous=form.select_one('[name=is_anonymous]').has_attr('checked'),
                            only_visible_to_student=form.select_one('[name=only_visible_to_student]').has_attr('checked')))


class ReviewPublisher:
    def __init__(self, session=None, transport=None, directory=None):
        self.session=session or load_session();self.transport=transport
        self.account=digest(self.session['user_id']);self.binding=digest(self.session['cookies'])
        self.store=PlanStore('icourse',self.account,directory)

    def request(self, course_id, payload=None):
        bounded(course_id,'course_id',100000000)
        cookies=httpx.Cookies()
        for c in self.session['cookies']:cookies.set(c['name'],c['value'],domain=c['domain'],path=c.get('path','/'))
        try:
            with http_client('icourse',ICourseError,cookies=cookies,transport=self.transport,headers={'User-Agent':self.session.get('user_agent','SchoolMCP/0.1'),'Origin':BASE,'Referer':BASE+f'/course/{course_id}/review/'},timeout=30,follow_redirects=False) as c:
                r=bounded_request(c,'POST' if payload is not None else 'GET',BASE+f'/course/{course_id}/review/',maximum_bytes=8*1024*1024,error_type=ICourseError,**({'data':payload} if payload is not None else {}))
                if r.status_code!=200:
                    raise ICourseError('点评页面或提交结果不可确认；先检查会话，不重发。')
                return r
        except httpx.HTTPError:
            raise ICourseError('评课请求失败；写入结果未知时只查状态。') from None

    def form(self, course_id):
        r=self.request(course_id)
        require('text/html' in r.headers.get('content-type',''), '点评页面类型变化。')
        result=form_snapshot(r.text,course_id)
        require(result['user_id']==str(self.session['user_id']), '评课账号已变化。')
        return result

    def options(self, course_id):
        f=self.form(course_id)
        return dict(course_id=course_id,course_title=f['course_title'],terms=f['terms'],existing_review=f['existing'],
                    can_create_new=not f['existing'],rating_scale='rate:1–10，页面星数为rate/2；四个维度各1–3，全部填写或全部留空。',
                    dimensions={'difficulty':['简单','中等','困难'],'homework':['不多','中等','超多'],'grading':['超好','一般','杀手'],'gain':['很多','一般','没有']},
                    next_action='review_existing_do_not_overwrite' if f['existing'] else 'prepare_user_authored_review',content_is_untrusted=True)

    def prepare(self, course_id, term, content, ratings=None, anonymous=False, students_only=False):
        require(isinstance(content,str) and 10<=len(content.strip())<=30000, '提供至少10字、最多30000字的本人评价，不编造修课经历。')
        require(type(anonymous) is bool and type(students_only) is bool, '匿名和可见性必须为明确布尔值。')
        require(isinstance(term,str) and term.isdigit(), '学期必须来自review_options。')
        ratings=ratings or {}
        require(not ratings or set(ratings)==set(RATINGS), '全部评分一起提供，或全部省略。')
        require(all(type(v) is int and 1<=v<=(10 if k=='rate' else 3) for k,v in ratings.items()), '评分超出范围。')
        f=self.form(course_id)
        if f['existing']:
            return dict(state='existing_review',can_execute=False,next_action='review_existing_do_not_overwrite',course_id=course_id)
        require(term in {t['id'] for t in f['terms']}, '学期不属于当前课程。')
        html_content='<p>'+html.escape(content.strip()).replace('\n','<br>')+'</p>'
        payload=dict(course_id=course_id,term=term,content=html_content,**{k:ratings.get(k) for k in RATINGS},is_anonymous=anonymous,only_visible_to_student=students_only)
        preview=dict(course_id=course_id,course_title=f['course_title'],term=next(t for t in f['terms'] if t['id']==term),content=content.strip(),ratings=ratings,anonymous=anonymous,students_only=students_only)
        return self.store.create(str(course_id),payload,{'existing':False,'terms':f['terms'],'course_title':f['course_title']},self.binding,preview)

    def verify(self, plan):
        p=plan['content'];f=self.form(p['course_id']);v=f['values']
        expected={k:p[k] for k in v}
        def canonical(value):
            return BeautifulSoup(value,'html.parser').get_text('\n').strip().replace('\r\n','\n')
        matches=f['existing'] and all(v[k]==expected[k] for k in v if k!='content') and canonical(v['content'])==canonical(expected['content'])
        return dict(matched=bool(matches),course_id=p['course_id'],source_url=BASE+f"/course/{p['course_id']}/",verified_fields=list(v) if matches else [])

    def publish(self, plan_id, content_sha256):
        if not self.store.claim(plan_id,content_sha256):return self.store.view(plan_id)
        wrote=False
        try:
            plan=self.store.get(plan_id)['plan'];p=plan['content'];f=self.form(p['course_id'])
            require(plan['session']==self.binding and not f['existing'] and f['terms']==plan['snapshot']['terms'] and f['course_title']==plan['snapshot']['course_title'], '会话、课程、学期或已有点评发生变化，停止发布。')
            payload={k:str(v) for k,v in p.items() if k not in {'course_id','is_anonymous','only_visible_to_student'} and v is not None}
            payload.update(csrf_token=f['csrf'],is_ajax='1')
            for k in ['is_anonymous','only_visible_to_student']:
                if p[k]:payload[k]='1'
            self.store.phase(plan_id,'publish_requested');wrote=True
            r=self.request(p['course_id'],payload)
            require('application/json' in r.headers.get('content-type',''), '发布结果不是预期JSON。')
            result=r.json()
            require(isinstance(result,dict) and result.get('ok') is True, '站点未确认点评发布。')
            target=urlsplit(urljoin(BASE,result.get('next_url','')))
            require(target.scheme=='https' and target.netloc=='icourse.club' and not target.query and target.path==f"/course/{p['course_id']}/" and re.fullmatch(r'review-\d+',target.fragment or ''), '回执目标与课程不符。')
            self.store.phase(plan_id,'readback');result=self.verify(plan)
            result['review_url']=target.geturl()
            self.store.finish(plan_id,'verified' if result['matched'] else 'uncertain',result)
        except Exception as exc:
            self.store.finish(plan_id,'uncertain' if wrote else 'stopped',dict(reason='verify_review_before_any_retry' if wrote else 'preflight_failed',detail=str(exc) if isinstance(exc,(WorkflowError,ICourseError)) else type(exc).__name__))
        return self.store.view(plan_id)

    def status(self, plan_id, verify=False):
        record=self.store.get(plan_id)
        if verify and (record['state']=='uncertain' or record['state']=='executing' and record['phase']=='readback'):
            result=self.verify(record['plan'])
            self.store.finish(plan_id,'verified' if result['matched'] else 'uncertain',result)
        return self.store.view(plan_id)
