"""Scheduled, account-bound, single-attempt registrations using headless Chrome.

Scheduling is external (for example a Codex thread heartbeat). This module never
starts a hidden recurring service. Unknown write outcomes permanently stop replay.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from .. import network
from ..bb.login import CONTEXT_OPTIONS
from ..browser import chrome_browser
from ..mail.config import local_dir
from ..mail.credentials import _dpapi
from .session import HOME, HOST, ORIGIN, YoungError, load_session

CN = timezone(timedelta(hours=8))
BASE = ORIGIN + '/login/wisdom-group-learning-bg'
DETAIL = '/item/scItem/queryById'
LIST = '/mobile/item/enrolmentList'
WRITE = '/item/scItemRegistration/enter/'
TERMINAL = {'verified', 'stopped', 'uncertain', 'expired', 'cancelled'}


def require(ok, message):
    if not ok:
        raise YoungError(message)


def stamp(value):
    try:
        d = datetime.fromisoformat(value)
        require(d.tzinfo is not None, '时间必须包含时区，例如 +08:00。')
        return d
    except (ValueError, TypeError):
        raise YoungError('时间格式无效。') from None


def school_time(value):
    try:
        d = datetime.fromisoformat(value)
        return d.replace(tzinfo=CN) if d.tzinfo is None else d
    except (TypeError, ValueError):
        raise YoungError('平台报名时间缺失或格式变化，停止报名。') from None


def clean_name(value):
    require(isinstance(value, str) and 4 <= len(value.strip()) <= 200, '需要完整的项目名称。')
    require(not re.search(r'[\x00-\x1f]|\.\.\.|…', value), '名称不能包含截断省略号或控制字符。')
    return value.strip()


def normalize_title(value):
    return ''.join(c for c in unicodedata.normalize('NFKC',str(value)).casefold() if c.isalnum())


def validate_keywords(values):
    require(isinstance(values,list) and 2<=len(values)<=8,'模糊匹配需要2–8个明确关键词。')
    require(all(isinstance(v,str) and 2<=len(normalize_title(v))<=80 for v in values),'关键词必须包含2–80个有效字符。')
    normalized=[normalize_title(v) for v in values]
    require(len(set(normalized))==len(values),'关键词不能重复。')
    return values


def matches_name(detail,job):
    name=detail.get('itemName') or ''
    if job.get('resolved_name') and name!=job['resolved_name']:return False
    if job.get('exact_name'):return name==job['exact_name']
    keywords=job.get('name_keywords')
    if not keywords:return False
    title=normalize_title(name)
    return all(normalize_title(word) in title for word in keywords)


class AmbiguousMatch(YoungError):
    def __init__(self,candidates):
        super().__init__('多个项目同时符合关键词与开放时间，已停止报名。')
        self.candidates=[summary(d) for d in candidates]


def summary(d):
    keys = ('id', 'itemName', 'applySt', 'applyEt', 'st', 'et', 'itemCategory',
            'form', 'formName', 'serviceHour', 'peopleNum', 'applyNum',
            'booleanRegistration', 'needSignInfo', 'signInfo', 'onlineStatus', 'marathon')
    return {k: d.get(k) for k in keys}


def check_candidate(detail, job, now):
    require(matches_name(detail,job), '项目名称不符合授权筛选条件。')
    require(not job.get('item_id') or detail.get('id') == job['item_id'], '项目标识不一致。')
    require(school_time(detail.get('applySt')) == stamp(job['opens_at']), '平台报名开始时间与授权时间不一致。')
    require(school_time(detail.get('applySt')) <= now <= school_time(detail.get('applyEt')), '不在平台报名窗口内。')
    if str(detail.get('booleanRegistration')) == '1':
        return 'already_registered'
    require(str(detail.get('booleanRegistration')) == '0', '无法确定本人已有报名状态。')
    for key in ('itemCategory', 'form', 'needSignInfo', 'onlineStatus', 'marathon'):
        require(str(detail.get(key)) == '0', f'项目 {key} 需要额外流程；当前仅自动报名普通线下单次活动且不代填附加资料。')
    if str(detail.get('signInfo')) == '1':
        require(job.get('allow_non_cancellable') is True, '本项目报名后不可取消，现有授权没有涵盖该条件。')
    require(str(detail.get('signInfo')) in {'0','1'}, '取消报名规则未知。')
    require(str(detail.get('examineStatus')) == '10' and str(detail.get('applyStatus')) == '26', '平台当前不允许报名。')
    text = BeautifulSoup(str(detail.get('baseContent') or '') + str(detail.get('conceive') or ''), 'html.parser').get_text(' ', strip=True)
    require(not re.search(r'缴费|付费|保证金|签署|必须.{0,8}(?:加群|另行报名)|仅限|限.{0,12}(?:年级|专业|学院|同学)|限额报名', text), '活动说明包含需额外核实的资格、费用或承诺；请人工核对。')
    return 'eligible'


class Jobs:
    def __init__(self, account, directory=None):
        self.account = hashlib.sha256(account.encode()).hexdigest()
        self.path = (directory or local_dir()) / 'young-registrations.sqlite3'

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=10)
        try:
            con.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, account TEXT, target TEXT, state TEXT, payload BLOB, result BLOB)')
            con.execute('BEGIN IMMEDIATE')
            yield con
            con.commit()
        finally:
            con.close()

    def create(self, name, opens_at, *, item_id=None, authorized=False, allow_non_cancellable=False,
               monitor_from=None, poll_seconds=10, stop_at=None, name_keywords=None):
        require(authorized is True, '只有用户明确授权该项目的定时报名后才能创建执行任务。')
        if name is not None:
            name=clean_name(name)
            require(name_keywords is None,'完整名称与模糊关键词只能选择一种。')
        else:name_keywords=validate_keywords(name_keywords)
        due = stamp(opens_at)
        require(datetime.now(timezone.utc) < due < datetime.now(timezone.utc)+timedelta(days=30), '开始时间必须在未来 30 天内。')
        require(item_id is None or isinstance(item_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', item_id), '活动标识无效。')
        require(isinstance(poll_seconds,int) and not isinstance(poll_seconds,bool) and 10<=poll_seconds<=300,'轮询间隔必须为10–300秒。')
        start=stamp(monitor_from) if monitor_from else None
        require(start is None or due-timedelta(days=1)<=start<=due,'监控开始时间必须在开放时间前24小时内。')
        end=stamp(stop_at) if stop_at else None
        require(end is None or end>due,'监控结束时间必须晚于开放时间。')
        job = dict(exact_name=name, name_keywords=name_keywords, opens_at=due.isoformat(), expires_at=end.isoformat() if end else None if start else (due+timedelta(minutes=15)).isoformat(),
                   monitor_from=start.isoformat() if start else None,poll_seconds=poll_seconds,
                   item_id=item_id, allow_non_cancellable=bool(allow_non_cancellable))
        target = hashlib.sha256(json.dumps([self.account,name or sorted(normalize_title(k) for k in name_keywords),due.isoformat()],ensure_ascii=False).encode()).hexdigest()
        with self.db() as db:
            old = db.execute('SELECT id FROM jobs WHERE target=? AND account=? AND state!=?', (target,self.account,'cancelled')).fetchone()
            if old:
                return self._view(db,old[0])
            identifier = uuid.uuid4().hex
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,NULL)', (identifier,self.account,target,'scheduled',_dpapi(json.dumps(job,ensure_ascii=False).encode())))
            return self._view(db,identifier)

    def _row(self, db, identifier):
        require(isinstance(identifier,str) and re.fullmatch('[a-f0-9]{32}',identifier), 'job_id 无效。')
        row = db.execute('SELECT state,payload,result FROM jobs WHERE id=? AND account=?',(identifier,self.account)).fetchone()
        require(row is not None, '任务不存在或不属于当前账号。')
        return row

    def _view(self, db, identifier):
        state,payload,result = self._row(db,identifier)
        return dict(job_id=identifier,state=state,**json.loads(_dpapi(payload,decrypt=True)),
                    result=json.loads(_dpapi(result,decrypt=True)) if result else None,
                    scheduler_installed=False, retry_write_allowed=False,
                    note='监控任务由系统计划任务启动脚本；状态及结果由本地程序记录。')

    def view(self, identifier):
        with self.db() as db:
            return self._view(db,identifier)

    def claim(self, identifier, now=None):
        now = now or datetime.now(timezone.utc)
        with self.db() as db:
            view = self._view(db,identifier)
            if view['state']!='scheduled' or now<stamp(view['opens_at']):
                return False
            if view['expires_at'] and now>stamp(view['expires_at']):
                db.execute("UPDATE jobs SET state='expired' WHERE id=?",(identifier,))
                return False
            db.execute("UPDATE jobs SET state='checking' WHERE id=?",(identifier,))
            return True

    def transition(self, identifier, expected, state, result=None):
        with self.db() as db:
            self._row(db,identifier)
            changed=db.execute('UPDATE jobs SET state=?, result=? WHERE id=? AND account=? AND state=?',
                (state,_dpapi(json.dumps(result,ensure_ascii=False).encode()) if result else None,identifier,self.account,expected)).rowcount
            require(changed==1, '任务状态已变化；不能重复执行。')

    def cancel(self, identifier):
        state=self.view(identifier)['state']
        require(state in {'scheduled','monitoring','checking'},'已提交或结果不明的报名不能通过取消监控任务撤回。')
        self.transition(identifier,state,'cancelled')
        return self.view(identifier)

    def claim_monitor(self,identifier,now=None):
        now=now or datetime.now(timezone.utc)
        with self.db() as db:
            view=self._view(db,identifier)
            if view['state']!='scheduled' or now<stamp(view.get('monitor_from') or view['opens_at']):return False
            if view['expires_at'] and now>stamp(view['expires_at']):
                db.execute("UPDATE jobs SET state='expired' WHERE id=?",(identifier,));return False
            db.execute("UPDATE jobs SET state='monitoring' WHERE id=?",(identifier,))
            return True

    def progress(self,identifier,result):
        with self.db() as db:
            self._row(db,identifier)
            db.execute("UPDATE jobs SET result=? WHERE id=? AND account=? AND state='monitoring'",(_dpapi(json.dumps(result,ensure_ascii=False).encode()),identifier,self.account))


class RegistrationBrowser:
    def __init__(self, session):
        self.session=session
        self.write_path=None
        self.before_write=None
        self.write_started=False
        self.write_response=None
        self.error=None
        self.policy_error=None
        self.token=None

    def guard(self, route):
        r=route.request;u=urlsplit(r.url)
        if u.scheme!='https' or u.hostname!=HOST or u.port not in (None,443):
            return route.abort()
        if r.method=='POST' and self.write_path and u.path==self.write_path and not self.write_started:
            try:
                network.limiter.acquire('young')
                self.before_write()
                self.write_started=True
                response=route.fetch(max_redirects=0,max_retries=0,timeout=15000)
                network.limiter.response('young',response.status,response.headers)
                require(response.status==200,'报名响应状态异常。')
                self.write_response=response.json()
                require(isinstance(self.write_response,dict),'报名响应格式异常。')
                route.fulfill(response=response)
            except Exception:
                self.error='报名响应未确认，禁止重新提交。'
                route.abort()
            return
        read_post=r.method=='POST' and u.path.endswith(('/lib/scItemProgramme/selectProgramme','/item/scItemPlace/list'))
        if (r.method not in ('GET','HEAD') and not read_post) or re.search(r'delete|save|submit|logout|approve|upload|cancel|create|/enter/|/add(?:/|$)',u.path,re.I):
            return route.abort()
        try:
            if '/wisdom-group-learning-bg/' in u.path:
                network.limiter.acquire('young')
            route.continue_()
        except network.PolicyError as exc:
            self.policy_error=exc
            self.error='平台正在冷却，停止本次请求。'
            route.abort()

    def observe(self,response):
        if '/wisdom-group-learning-bg/' not in urlsplit(response.url).path:
            return
        try:
            network.limiter.response('young',response.status,response.headers)
        except network.PolicyError as exc:
            self.policy_error=exc
            self.error='平台限流或失败冷却中，停止后续操作。'

    @contextmanager
    def open(self):
        options={**CONTEXT_OPTIONS,'storage_state':self.session['state'],'service_workers':'block','accept_downloads':False}
        if self.session.get('user_agent'):options['user_agent']=self.session['user_agent']
        with sync_playwright() as pw,chrome_browser(pw,headless=True) as browser:
            self.context=browser.new_context(**options)
            self.context.route('**/*',self.guard)
            self.page=self.context.new_page();self.page.on('response',self.observe)
            self.page.goto(HOME,wait_until='domcontentloaded',timeout=30000)
            self.page.get_by_text('欢迎您',exact=False).first.wait_for(timeout=20000)
            require(urlsplit(self.page.url).hostname==HOST,'青春科大会话失效。')
            yield self

    def start_listing(self):
        p=self.page
        if getattr(self,'listing_started',False):
            with p.expect_response(lambda r:urlsplit(r.url).path.endswith(LIST),timeout=30000) as first:
                p.reload(wait_until='domcontentloaded')
                p.get_by_text('报名中',exact=True).click()
            response=first.value
            self.token=response.request.header_value('X-Access-Token')
            return response
        p.get_by_text('项目管理',exact=True).click()
        p.get_by_text('我要报名',exact=True).click()
        with p.expect_response(lambda r:urlsplit(r.url).path.endswith(LIST),timeout=30000) as first:
            p.get_by_text('报名中',exact=True).click()
        response=first.value
        self.token=response.request.header_value('X-Access-Token')
        self.listing_started=True
        return response

    def listing(self, keyword, *, max_pages=30):
        p=self.page
        response=self.start_listing()
        records=[];seen=set()
        for _ in range(max_pages):
            if self.policy_error:raise self.policy_error
            require(self.error is None,self.error or '')
            data=response.json()
            require(isinstance(data,dict) and data.get('success') is True,'活动列表读取失败，不能视为空结果。')
            result=data.get('result') or {};batch=result.get('records')
            require(isinstance(batch,list),'活动列表格式变化。')
            ids=tuple(r.get('id') for r in batch)
            require(not batch or ids not in seen,'分页没有前进，停止查找。')
            seen.add(ids);records.extend(batch)
            next_page=p.locator('.ant-pagination-next:visible')
            next_page.wait_for(timeout=5000)
            if 'ant-pagination-disabled' in (next_page.get_attribute('class') or ''):
                return [summary(r) for r in records if normalize_title(keyword) in normalize_title(r.get('itemName',''))]
            with p.expect_response(lambda r:urlsplit(r.url).path.endswith(LIST),timeout=30000) as nxt:next_page.click()
            response=nxt.value
        raise YoungError('活动列表超过读取上限，无法保证匹配唯一。')

    def detail(self, item_id):
        require(self.token,'缺少当前会话请求凭据。')
        network.limiter.acquire('young')
        r=self.context.request.get(BASE+DETAIL,params={'id':item_id},headers={'X-Access-Token':self.token},max_redirects=0,timeout=15000)
        network.limiter.response('young',r.status,r.headers)
        require(r.status==200,'活动详情读取失败。')
        d=r.json()
        require(isinstance(d,dict) and d.get('success') is True and isinstance(d.get('result'),dict),'活动详情无有效数据。')
        require(d['result'].get('id')==item_id,'活动详情标识不一致。')
        return d['result']

    def locate(self, job, *, allow_missing=False):
        records=self.listing(job.get('exact_name') or job['name_keywords'][0])
        matches=[r for r in records if matches_name(r,job) and (not job.get('item_id') or r.get('id')==job['item_id'])]
        # Resolve times from authoritative details, rather than guessing which
        # similar title the user meant or relying on list ordering.
        if not job.get('exact_name'):
            matches=[d for row in matches if school_time((d:=self.detail(row['id'])).get('applySt'))==stamp(job['opens_at']) and matches_name(d,job)]
        if not matches and allow_missing:return None
        if len(matches)>1:raise AmbiguousMatch(matches)
        require(len(matches)==1,'未找到唯一的指定活动；不提交报名。')
        item_id=matches[0]['id']
        title_name=matches[0]['itemName']
        # Search input currently returns server errors; use observed pagination.
        p=self.page
        first=p.locator('.ant-pagination-item-1:visible')
        if 'ant-pagination-item-active' not in (first.get_attribute('class') or ''):
            with p.expect_response(lambda r:urlsplit(r.url).path.endswith(LIST),timeout=30000):first.click()
        for _ in range(30):
            title=p.get_by_text(title_name,exact=True).filter(visible=True)
            if title.count()==1:
                with p.expect_response(lambda r:urlsplit(r.url).path.endswith(DETAIL),timeout=20000) as opened:title.click()
                d=opened.value.json()
                require(d.get('success') is True and d.get('result',{}).get('id')==item_id,'页面活动与目标不一致。')
                self.page.locator('.ant-modal:visible').wait_for()
                return d['result']
            nxt=p.locator('.ant-pagination-next:visible')
            require('ant-pagination-disabled' not in (nxt.get_attribute('class') or ''),'目标页面不可见。')
            with p.expect_response(lambda r:urlsplit(r.url).path.endswith(LIST),timeout=30000):nxt.click()
        raise YoungError('无法定位项目页面。')

    def submit(self, item_id, before_write):
        self.before_write=before_write
        self.write_path='/login/wisdom-group-learning-bg'+WRITE+item_id
        modal=self.page.locator('.ant-modal:visible')
        button=modal.get_by_role('button',name=re.compile(r'^报\s*名$'))
        require(button.count()==1 and button.is_enabled(),'没有可用报名按钮。')
        button.click()
        pop=self.page.locator('.ant-popover:visible')
        pop.wait_for(timeout=5000)
        text=pop.inner_text()
        require('确定报名吗' in text,'报名确认界面变化，停止。')
        confirm=pop.get_by_role('button',name=re.compile(r'^确\s*定$'))
        require(confirm.count()==1,'报名确认按钮不唯一。')
        confirm.click()
        self.page.wait_for_timeout(1000)
        require(self.error is None,self.error or '')
        require(self.write_started and isinstance(self.write_response,dict),'未确认报名响应。')
        return self.write_response

    def registration_button_ready(self):
        button=self.page.locator('.ant-modal:visible').get_by_role('button',name=re.compile(r'^报\s*名$'))
        return button.count()==1 and button.is_enabled()


def create_job(exact_name, opens_at, item_id=None, authorized=False, allow_non_cancellable=False):
    s=load_session()
    return Jobs(s['account']).create(exact_name,opens_at,item_id=item_id,authorized=authorized,allow_non_cancellable=allow_non_cancellable)


def status_job(job_id, verify=False):
    s=load_session();store=Jobs(s['account']);job=store.view(job_id)
    if not verify or job['state'] not in {'submitting','uncertain'}:
        return job
    result=job.get('result') or {}
    item_id=result.get('item_id') or result.get('item',{}).get('id')
    require(item_id,'没有可独立核验的活动标识；不可重新提交。')
    try:
        with RegistrationBrowser(s).open() as browser:
            browser.start_listing()
            after=browser.detail(item_id)
            require(matches_name(after,job),'核验名称不一致。')
            if str(after.get('booleanRegistration'))=='1':
                store.transition(job_id,job['state'],'verified',dict(item=summary(after),registration_verified=True,credit_hours_awarded=False))
    except YoungError:
        raise
    except Exception:
        raise YoungError('报名核验未完成，仍不可重发。') from None
    return store.view(job_id)


def cancel_job(job_id):
    return Jobs(load_session()['account']).cancel(job_id)


def find_projects(keyword):
    require(isinstance(keyword,str) and 1<=len(keyword)<=200,'需要 1–200 字的查询词。')
    try:
        with RegistrationBrowser(load_session()).open() as browser:
            return dict(projects=browser.listing(keyword),scope='当前报名中项目',content_is_untrusted=True)
    except YoungError:
        raise
    except Exception:
        raise YoungError('项目查询未完成；请检查连接或冷却状态。') from None


def run_job(job_id):
    s=load_session();store=Jobs(s['account'])
    if not store.claim(job_id):return store.view(job_id)
    job=store.view(job_id)
    try:
        with RegistrationBrowser(s).open() as browser:
            detail=browser.locate(job)
            verdict=check_candidate(detail,job,datetime.now(timezone.utc))
            if verdict=='already_registered':
                store.transition(job_id,'checking','verified',dict(already_registered=True,item=summary(detail)))
                return store.view(job_id)
            def before_write():
                now=datetime.now(timezone.utc)
                require(stamp(job['opens_at'])<=now and (not job['expires_at'] or now<=stamp(job['expires_at'])),'定时执行窗口已过，停止提交。')
                require(now<=school_time(detail['applyEt']),'活动报名已经截止。')
                store.transition(job_id,'checking','submitting',dict(item_id=detail['id']))
            result=browser.submit(detail['id'],before_write)
            # Independently query this account's registration flag even for an error response.
            after=browser.detail(detail['id'])
            verified=str(after.get('booleanRegistration'))=='1'
            store.transition(job_id,'submitting','verified' if verified else 'uncertain',
                             dict(item=summary(after),response_success=result.get('success') is True,
                                  registration_verified=verified,credit_hours_awarded=False))
    except Exception as exc:
        state=store.view(job_id)['state']
        if state in {'checking','submitting'}:
            previous=store.view(job_id).get('result') or {}
            store.transition(job_id,state,'uncertain' if state=='submitting' else 'stopped',
                dict(**previous,reason=str(exc) if isinstance(exc,YoungError) else '后台请求未完成；检查连接或冷却状态。',retry_write_allowed=False))
    return store.view(job_id)
