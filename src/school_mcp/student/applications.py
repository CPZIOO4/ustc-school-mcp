"""Frozen, account-bound undergraduate scholarship applications, driven by observed UI."""
from contextlib import contextmanager
from datetime import datetime
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from ..bb.identity import load_credentials
from ..campus.session import browser_session
from ..desktop.storage import read_private, write_private
from ..input_guidance import field, guidance
from ..mail.config import local_dir
from ..network import limiter
from ..safety_log import record
from ..service_errors import ServiceError
from ..workflow_store import PlanStore, digest
from ..digest.calendar import CST
from .client import connected, enter_scholarships, PAGES

TITLE = '中国科大优秀学生奖学金'
HOST = 'xgyth.ustc.edu.cn'
FORM = '/jxj/scholarship/pbApplyTable.aspx'
SAVE = '/jxj/scholarship/pbApplyTableSave.aspx'
FINISH = '/jxj/scholarship/pbApplyTableFinish.aspx'
# Labels are selected from the live DOM, never converted to guessed school IDs.
FIELDS = {
    'nation': ('Select2', '民族', True),
    'political_status': ('Select3', '政治面貌', True),
    'entry_month': ('Text4', '入学年月（YYYY-MM）', True),
    'email': ('Text5', '联系邮箱', True),
    'campus_address': ('Text7', '校内住址', True),
    'phone': ('Text8', '联系电话', True),
    'school_zip': ('Text6', '毕业高中邮编', True),
    'school_province': ('Text9', '高中所在省市', False),
    'school_city': ('Text15', '高中所在市县区', False),
    'school_name': ('Text16', '高中名称（表单自带“中学”后缀）', True),
    'cadre': ('Textarea1', '学生干部情况', False),
    'clubs': ('Textarea2', '学生社团情况', False),
    'awards': ('Textarea3', '大学期间获奖情况', False),
    'statement': ('Textarea4', '在校综合表现（至少150字）', True),
}
ACTIVITIES = ('美国大学生数模竞赛', '中国大学生数学竞赛', '志愿服务', '社会调研',
              '暑期挂职锻炼', '党校', '团校', '社会工作训练营', '学生骨干理论培训班')
NOTE = '当前个人表单不提供金银铜等级选择，金额只读；requested_level仅记录申请意向，不代表等级已选定或获奖。'


def fail(message):
    raise ServiceError(message, code='invalid_arguments')


def form_url(url):
    p = urlsplit(urljoin('https://' + HOST + FORM, url))
    q = parse_qs(p.query, keep_blank_values=True)
    if (p.scheme != 'https' or p.hostname != HOST or p.port not in (None, 443) or
            p.username or p.password or p.fragment or p.path != FORM or set(q) != {'itemID'} or
            len(q['itemID']) != 1 or not re.fullmatch(r'[1-9][0-9]{0,24}', q['itemID'][0])):
        fail('申请入口不符合已验证的官方表单格式。')
    return 'https://' + HOST + FORM + '?itemID=' + q['itemID'][0]


def remember(account, name, values, links):
    if name != TITLE or len(values) != 4 or not re.fullmatch(r'20\d\d', values[0]):
        return {}
    urls = []
    for _, url in links:
        try: urls.append(form_url(url))
        except (ServiceError, ValueError): pass
    if len(set(urls)) != 1:
        return {}
    item = dict(name=name, year=values[0], starts=values[1], ends=values[2], url=urls[0])
    path = local_dir() / 'student-application-references.dpapi'
    old = read_private(path, {})
    refs = old.get('refs', {}) if old.get('account') == account else {}
    ref = digest(item)[:24]; refs[ref] = item
    write_private(path, {'account': account, 'refs': dict(list(refs.items())[-500:])})
    return {'application_reference': ref, 'application_supported': True,
            'opening_creates_draft': True, 'application_status': values[3]}


def reference(ref):
    if not isinstance(ref, str) or not re.fullmatch('[a-f0-9]{24}', ref):
        fail('请使用奖学金列表返回的 application_reference。')
    account = load_credentials()['username']
    saved = read_private(local_dir() / 'student-application-references.dpapi', {})
    item = saved.get('refs', {}).get(ref) if saved.get('account') == account else None
    if not item: fail('申请引用不属于当前账号，请重新查询列表。')
    form_url(item['url'])
    return item


def store():
    return PlanStore('student', digest(load_credentials()['username']))


def guide():
    return {'supported_project': TITLE,
            'fields': {k: {'label': v[1], 'required': v[2]} for k,v in FIELDS.items()},
            'activities': list(ACTIVITIES), 'statement_min_chars': 150,
            'flow': ['scholarships', 'application_guide', 'prepare_application', 'submit_application', 'application_status'],
            'opening_creates_draft': True, 'fields_autosave': True,
            'note': NOTE, 'unsupported': ['其他奖学金模板', '附件上传', '修改已提交申请', '改变申报班级'],
            'content_rule': '以用户最新提供的事实和截图为准；不得推断民族、政治面貌、获奖等级或用无填补未知。'}


def validate(values, activities):
    if not isinstance(values, dict) or set(values) - set(FIELDS):
        fail('fields 含未知字段；先读 application_guide。')
    errors = []
    for key, (_, label, required) in FIELDS.items():
        value = values.get(key)
        if value is None or isinstance(value, str) and not value.strip():
            if required: errors.append(field('/fields/'+key, '请补充'+label, label))
            continue
        if not isinstance(value, str) or len(value) > (5000 if key in {'cadre','clubs','awards','statement'} else 180) or re.search(r'[\x00-\x08\x0b-\x1f]', value):
            fail('字段格式或长度无效：'+label)
    if values.get('statement') and len(re.sub(r'\s', '', values['statement'])) < 150:
        errors.append(field('/fields/statement', '综合表现不足150字，请根据已有事实补充，不能编造经历。', '150–5000字', code='too_short'))
    patterns = {'entry_month': r'(19|20)\d{2}-(0[1-9]|1[0-2])', 'school_zip': r'\d{6}',
                'email': r'[^\s@]+@[^\s@]+\.[^\s@]+', 'phone': r'[0-9+() -]{6,30}'}
    for key, pattern in patterns.items():
        if values.get(key) and not re.fullmatch(pattern, values[key]):
            errors.append(field('/fields/'+key, '请核对'+FIELDS[key][1], FIELDS[key][1], code='invalid_format'))
    if not isinstance(activities, list) or any(a not in ACTIVITIES for a in activities) or len(set(activities)) != len(activities):
        fail('activities 必须是指南中的不重复活动名称；不得把参赛写成获奖。')
    return errors


def prepare(application_reference, fields, activities=None, requested_level='铜奖'):
    target = reference(application_reference)
    activities = activities or []
    if requested_level not in {'金奖','银奖','铜奖','未指定'}: fail('申请意向为金奖、银奖、铜奖或未指定。')
    errors = validate(fields, activities)
    if errors:
        return {'state': 'needs_input', 'school_mutated': False,
                'input_guidance': guidance('school_student_prepare_application', errors,
                    preserve=('application_reference','fields','activities','requested_level'),
                    arguments={'application_reference': application_reference})}
    # Freeze the complete submitted text. Empty optional values are visible in
    # preview, never silently converted to invented "none" or old chat history.
    fields = {key: fields.get(key) or '' for key in FIELDS}
    content = {'target': target, 'fields': fields, 'activities': activities, 'requested_level': requested_level}
    preview = {'project': target['name'], 'year': target['year'], 'fields': fields, 'activities': activities,
               'requested_level': requested_level, 'note': NOTE, 'opens_and_saves_draft': True,
               'will_submit': True, 'attachments': [], 'school_mutated': False,
               'optional_empty_fields': [k for k,v in fields.items() if not v]}
    return store().create(target['url'], content, {}, {}, preview)


def rows(page):
    page.locator('#oHtmlTable').wait_for(state='attached', timeout=12000)
    result = []
    for tr in BeautifulSoup(page.content(), 'html.parser').select('#oHtmlTable tr'):
        cells = [c for c in tr.find_all(['td','th'], recursive=False)
                 if 'display:none' not in (c.get('style') or '').replace(' ','').lower()]
        values = [c.get_text(' ',strip=True) for c in cells]
        if len(values) != 6 or not values[0].isdigit(): continue
        for a in tr.select('a[href]'):
            try: url = form_url(a['href'])
            except (ServiceError, ValueError): continue
            result.append({'url':url,'name':values[1],'year':values[2],'starts':values[3],
                           'ends':values[4],'status':values[5]})
    return result


class BrowserApplication:
    def __init__(self, session):
        self.s = session
        enter_scholarships(session, connected(session))
        self.mode = 'read'; self.sent = 0; self.dialogs = []; self.blocked = False

    def lookup(self, target):
        found = []
        for kind in ('applied','open'):
            for row in rows(self.s.goto(PAGES[kind])):
                if row['url'] == target['url']: found.append(row)
        if len(found) != 1 or any(found[0].get(k) != target[k] for k in ('name','year','starts','ends')):
            fail('申请项目已变化或列表不明确，停止并重新核对。')
        return found[0]

    def guard(self, route):
        request = route.request; p = urlsplit(request.url)
        params = parse_qs(p.query + '&' + (request.post_data or ''), keep_blank_values=True)
        allow = p.scheme == 'https' and p.hostname == HOST and p.port in (None,443)
        if p.path == SAVE:
            action = params.get('funID')
            allow = allow and ((self.mode == 'fill' and action == ['2']) or
                               (self.mode == 'submit' and action == ['4'] and self.sent == 0))
            if allow and self.mode == 'submit': self.sent += 1
        elif p.path == '/jxj/service/const.aspx':
            allow = allow and params.get('funID') in (['1'],['2'])
        elif request.method not in ('GET','HEAD'):
            allow = False
        elif p.path in {FORM,FINISH}:
            target = urlsplit(self.target_url)
            allow = allow and p.query == target.query
        elif p.path.endswith('.aspx'):
            allow = allow and p.path in {FORM, FINISH, *PAGES.values()}
        if not allow:
            self.blocked = True; return route.abort()
        return route.fallback()

    def dialog(self, d):
        self.dialogs.append({'type':d.type,'text':d.message[:1000]})
        if self.mode == 'submit' and d.type == 'confirm' and d.message == '提交之后，将不可修改，确定要提交吗':
            d.accept()
        else: d.dismiss()

    def open(self, target):
        self.target_url = target['url']
        self.s.context.route('**/*', self.guard)
        self.page = self.s.goto(target['url'])
        self.page.on('dialog', self.dialog)
        # A stale login or another template must never receive these field values.
        if self.page.locator('#oScholarShipItemTypeName').input_value() != TITLE:
            fail('表单模板尚未适配。')
        if self.page.locator('#Text3').input_value().casefold() != self.s.account.casefold():
            fail('表单账号不匹配。')
        if not self.page.locator('[fieldname="BelongToClassID"]').input_value():
            fail('官方申报班级为空，不能自行猜测。')
        if not self.page.locator('#oApplyMoney').evaluate('(e)=>e.readOnly'):
            fail('奖金额控件已变化，需要核实新的表单规则。')
        for control in ('Text13','Text10','Text11','Text12','Text14','oScholarShipAdjunctID'):
            if self.page.locator('#'+control).input_value():
                fail('原草稿含此流程未适配的科研、其他活动或附件内容；保留原稿，不自动覆盖。')

    def snapshot(self):
        p = self.page
        controls = p.locator('input,select,textarea').evaluate_all('''els=>els.filter(e=>e.id||e.getAttribute('fieldname')).map(e=>({id:e.id,value:e.value,required:e.getAttribute('ismustfill')==='1',field:e.getAttribute('fieldname'),checked:e.type==='checkbox'&&e.checked,label:e.tagName==='SELECT'?e.options[e.selectedIndex]?.text:undefined}))''')
        return {x['id'] or x['field']: x for x in controls}

    def choices(self):
        return self.page.locator('input[type=checkbox]').evaluate_all('''es=>es.map(e=>({id:e.id,label:(e.parentElement.nextElementSibling?.textContent||'').trim().replace(/[；;]$/,''),checked:e.checked}))''')

    def fill(self, content):
        p = self.page; values = content['fields']; self.mode = 'fill'
        for key in ('nation','political_status'):
            limiter.acquire('student')
            p.locator('#'+FIELDS[key][0]).select_option(label=values[key])
        if p.locator('#Text4').input_value() != values['entry_month']:
            p.locator('#Text4').click(); frame = p.frame(name='YMLayer')
            year = int(frame.locator('#yearSpan').inner_text().removesuffix('年'))
            goal_year,month = map(int,values['entry_month'].split('-'))
            if abs(year-goal_year) > 15: fail('入学年月超出此控件已验证的选择范围。')
            for _ in range(abs(year-goal_year)):
                frame.get_by_title('向前翻 1 年' if goal_year < year else '向后翻 1 年',exact=True).first.click()
            if frame.locator('#yearSpan').inner_text() != str(goal_year)+'年': fail('入学年份未选中。')
            limiter.acquire('student'); frame.get_by_text(str(month)+'月',exact=True).click()
        for key,value in values.items():
            if key in {'nation','political_status','entry_month'}: continue
            limiter.acquire('student')
            item = p.locator('#'+FIELDS[key][0]); item.fill(value); item.press('Tab')
        choices = self.choices()
        for label in content['activities']:
            if len([c for c in choices if c['label']==label]) != 1: fail('活动选项已变化，停止而不猜测勾选位置。')
        for choice in choices:
            expected = choice['label'] in content['activities']
            if choice['checked'] != expected:
                limiter.acquire('student'); p.locator('#'+choice['id']).set_checked(expected)
        limiter.acquire('student')
        p.get_by_role('button',name='暂 存',exact=True).click()
        if self.blocked or not any(d['text']=='保存成功' for d in self.dialogs):
            fail('保存未确认；不进入提交。')
        p.reload(wait_until='domcontentloaded')
        self.assert_saved(content)

    def assert_saved(self, content):
        current = self.snapshot()
        for key,value in content['fields'].items():
            control = current[FIELDS[key][0]]
            actual = control.get('label') if key in {'nation','political_status'} else control['value']
            if actual != value: fail('重新读取的字段与原计划不一致：'+FIELDS[key][1])
        if {c['label'] for c in self.choices() if c['checked']} != set(content['activities']):
            fail('重新读取的勾选活动与原计划不一致。')
        if any(c['required'] and not c['value'] for c in current.values()):
            fail('网站存在未补齐的必填项，不能提交。')

    def submit(self):
        self.mode = 'submit'; self.dialogs = []
        limiter.acquire('student')
        self.page.get_by_role('button',name='提 交',exact=True).click(timeout=20000)
        return {'request_count':self.sent,
                'success_alert':any(d['text']=='提交成功！' for d in self.dialogs),
                # Only a recognized validation receipt is definite rejection.
                'validation_rejected':any(d['text']=='在校综合表现，字数不能低于150字！' for d in self.dialogs)}


@contextmanager
def browser():
    with browser_session('student') as s:
        yield BrowserApplication(s)


def check_window(target):
    now = datetime.now(CST)
    try: first = datetime.strptime(target['starts'],'%Y-%m-%d').date(); last = datetime.strptime(target['ends'],'%Y-%m-%d').date()
    except ValueError: fail('项目开放时间无法核实。')
    if not first <= now.date() <= last or now.date()==last and now.hour>=23:
        fail('不在已验证申请时段内。')


def submit(plan_id, content_sha256, authorized=False):
    if authorized is not True:
        raise ServiceError('须有本次项目与填写内容的提交授权；已有授权无需重复询问。',code='authorization_required')
    saved = store(); item = saved.get(plan_id); content = item['plan']['content']
    if item['state'] != 'ready': return saved.view(plan_id)
    if item['sha256'] != content_sha256: fail('申请材料摘要不匹配。')
    check_window(content['target'])
    if validate(content['fields'],content['activities']): fail('原计划内容未通过校验。')
    with browser() as b:
        row = b.lookup(content['target'])
        if row['status'] not in {'个人未提交','点击申请'}:
            return {'state':'already_submitted' if row['status']=='个人已提交' else 'unsupported_status',
                    'plan_id':plan_id,'official_status':row['status'],'school_mutated':False,
                    'note':'已有申请状态，本工具不修改已提交申请。'}
        if not saved.claim(plan_id,content_sha256): return saved.view(plan_id)
        try:
            saved.phase(plan_id,'opening_draft')
            b.open(content['target'])
            saved.phase(plan_id,'saving_fields')
            b.fill(content)
            saved.checkpoint(plan_id,{'saved_and_reloaded':True,'submitted':False})
            saved.phase(plan_id,'submitting')
            receipt = b.submit()
            saved.checkpoint(plan_id,receipt | {'saved_and_reloaded':True})
            row = b.lookup(content['target'])
            if row['status']=='个人已提交':
                saved.finish(plan_id,'verified',{'submitted':True,'official_status':row['status'],
                    'saved_and_reloaded':True,'award_level_confirmed':False,'note':NOTE})
            elif receipt['validation_rejected'] and row['status']=='个人未提交':
                saved.finish(plan_id,'stopped',{'submitted':False,'validation_rejected':True,
                    'official_status':row['status'],'note':'服务器确认字数不足且列表仍未提交；修改原内容后重新准备。'})
            else:
                saved.finish(plan_id,'uncertain',{'official_status':row['status'],'note':'没有一致的提交回执；只回查原计划。'})
        except Exception as exc:
            incident = record(service='student',stage='tool',exc=exc,outcome='unknown')
            saved.finish(plan_id,'uncertain',{'note':'可能已建草稿或保存部分内容，只回查原计划；不自动重发。',
                'error_id':incident['error_id']})
    return saved.view(plan_id)


def status(plan_id, verify=True):
    saved = store(); item = saved.get(plan_id)
    if verify and item['state'] in {'executing','uncertain'}:
        with browser() as b: row = b.lookup(item['plan']['content']['target'])
        if row['status']=='个人已提交':
            saved.finish(plan_id,'verified',{'submitted':True,'official_status':row['status'],
                'saved_and_reloaded':bool((item['result'] or {}).get('saved_and_reloaded')),
                'award_level_confirmed':False,'note':'官方列表已提交；不代表获奖。'+NOTE})
        else:
            result = saved.view(plan_id); result['official_status'] = row['status']
            result['note'] = '列表尚不能确认提交；保留执行锁，不能重发或新建计划绕过。'
            return result
    return saved.view(plan_id)
