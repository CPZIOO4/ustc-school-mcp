"""Direct campus pages: no model calls and no writes to school records."""
import re
from datetime import datetime

from .storage import read_private, write_private
from ..bb.identity import load_credentials
from ..digest.calendar import CST
from ..jw.client import JWClient, validate_id

# Stable entry pages, never a personal deep link, CAS ticket or application form.
SITES = (
    ('jw', '教务系统', '课程与成绩', 'https://jw.ustc.edu.cn/home', '教学'),
    ('bb', 'Blackboard', '课程资料与作业', 'https://www.bb.ustc.edu.cn/webapps/login/', '教学'),
    ('teach', '教务处', '教学通知', 'https://www.teach.ustc.edu.cn/', '教学'),
    ('icourse', '评课社区', '课程评价与经验', 'https://icourse.club/', '教学'),
    ('student', '学工一体化', '奖学金与学生事务', 'https://xgyth.ustc.edu.cn/', '办事'),
    ('portal', '一网通办', '服务大厅与待办', 'https://i.ustc.edu.cn/page/site/index', '办事'),
    ('young', '青春科大', '第二课堂与活动', 'https://young.ustc.edu.cn/login/sc-wisdom-group-learning/dataAnalysis/visual', '办事'),
    ('finance', '财务处', '财务通知与指南', 'https://finance.ustc.edu.cn/', '办事'),
    ('reimbursement', '智能报销', '财务综合服务', 'https://cwzh.ustc.edu.cn/YBX/main.jsp', '办事'),
    ('mail', '学校邮箱', '邮件与通讯', 'https://mail.ustc.edu.cn/', '校园'),
    ('library', '图书馆', '馆藏与资源', 'https://lib.ustc.edu.cn/', '校园'),
    ('spaces', '学习空间', '座位与讨论室', 'https://ic.lib.ustc.edu.cn/', '校园'),
    ('nan7', '南七集市', '校内二手交易', 'https://nan7market.com/', '校园'),
    ('drive', '科大云盘', '文件存储与共享', 'https://pan.ustc.edu.cn/profile/index', '资源'),
    ('compute', '107 算力平台', '计算资源与作业状态', 'https://107.ustc.edu.cn/', '资源'),
    ('vlab', 'Vlab', '虚拟机与实验环境', 'https://vlab.ustc.edu.cn/vm/', '资源'),
    ('llm', '科大大模型', '学校模型服务', 'https://llm.ustc.edu.cn/', '资源'),
    ('navigation', 'USTC 导航', '更多校园入口', 'https://ustc.life/', '资源'),
)
PROFILE_FIELDS = {
    'name': ('姓名', 60), 'college': ('院系', 100), 'major': ('专业', 100),
    'entry_month': ('入学年月', 7), 'email': ('联系邮箱', 254),
    'phone': ('联系电话', 30), 'campus_address': ('校内住址', 180),
}


def timestamp():
    return datetime.now(CST).isoformat(timespec='seconds')


def sites():
    return {'sites': [dict(zip(('id','name','description','url','category'), item)) for item in SITES]}


def private_record(data, filename):
    account = load_credentials()['username']
    saved = read_private(data.profile / filename, {})
    if saved and saved.get('account') != account:
        raise ValueError('本机资料属于另一个账号，请先在设置中切换账号。')
    return account, saved


def profile(data):
    account, saved = private_record(data, 'personal-profile.dpapi')
    return {'account': account, 'account_source': '本机已绑定的学校登录账号',
            'fields': {k: {'label': v[0], 'max_length': v[1]} for k,v in PROFILE_FIELDS.items()},
            'values': saved.get('values', {}), 'revision': saved.get('revision', 0),
            'updated_at': saved.get('updated_at'), 'values_source': '本人填写，未经学校核验'}


def save_profile(data, values, revision):
    account, old = private_record(data, 'personal-profile.dpapi')
    if type(revision) is not int or revision != old.get('revision', 0):
        raise ValueError('资料已在其他页面更新，请重新读取后再保存。')
    if not isinstance(values, dict) or set(values) != set(PROFILE_FIELDS):
        raise ValueError('请提供页面中的全部资料字段，未知项可留空。')
    clean = {}
    for key, (label, limit) in PROFILE_FIELDS.items():
        v = values[key]
        if not isinstance(v, str) or len(v) > limit or re.search(r'[\x00-\x1f]', v):
            raise ValueError(label+'格式或长度无效。')
        clean[key] = v.strip()
    patterns = {'entry_month': r'(19|20)\d{2}-(0[1-9]|1[0-2])',
                'email': r'[^\s@]+@[^\s@]+\.[^\s@]+', 'phone': r'[0-9+() -]{6,30}'}
    for key, pattern in patterns.items():
        if clean[key] and not re.fullmatch(pattern, clean[key]):
            raise ValueError('请核对'+PROFILE_FIELDS[key][0]+'格式。')
    write_private(data.profile/'personal-profile.dpapi', {'account': account, 'values': clean,
                  'revision': revision+1, 'updated_at': timestamp()})
    return profile(data)


def timetable(data):
    _, saved = private_record(data, 'timetable-view.dpapi')
    return {k:v for k,v in saved.items() if k != 'account'} | {'cached': bool(saved)}


def refresh_timetable(data, semester_id=0):
    validate_id(semester_id, 'semester_id')
    account, _ = private_record(data, 'timetable-view.dpapi')
    client = JWClient()
    semesters = client.semesters()
    selected = semester_id or semesters['current_semester_id']
    if selected not in {s['id'] for s in semesters['semesters']}:
        raise ValueError('学期不在学校返回的范围，请刷新当前学期后重新选择。')
    result = client.timetable(selected)
    if result['semester']['id'] != selected:
        raise ValueError('学校返回的课表学期与所选学期不同，保留原快照。')
    if load_credentials()['username'] != account:
        raise ValueError('账号已变化，不能保存本次课表。')
    write_private(data.profile/'timetable-view.dpapi', {'account': account,
                  'semesters': semesters['semesters'], 'data': result,
                  'updated_at': timestamp(), 'source_url': 'https://jw.ustc.edu.cn/home'})
    return {'saved': True, 'count': result['count'], 'semester': result['semester'],
            'note': '已更新课表快照，可按周查看；未修改选课。', 'model_used': False}
