"""One-stop portal service directory and personal workflow summaries."""
from bs4 import BeautifulSoup
from ..campus.session import browser_session
from ..service_errors import ServiceError
from ..teach.client import public_link


def plain(value):
    return BeautifulSoup(str(value or ''), 'html.parser').get_text(' ', strip=True)


def request(session, path, **params):
    result = session.get(path, {'clenttype': 'pc', **params}).json()
    if not isinstance(result, dict) or result.get('e') != 0:
        raise ServiceError('一网通办查询未成功，不能将其当作空结果。', code='authentication_required' if isinstance(result, dict) and result.get('e') in (401, -1) else 'unavailable')
    return result['d']


def connected(session):
    observed = []
    def capture(response):
        if response.url.split('?')[0] == session.base + '/site/user_info':
            try:
                value = response.json()
                if value.get('e') == 0:
                    observed.append(value['d'])
            except (ValueError, KeyError):
                pass
    session.context.on('response', capture)
    page = session.goto()
    if '/visitor' in page.url:
        page.get_by_text('登录', exact=True).first.click(timeout=8000)
        page.wait_for_url('**/page/site/index', timeout=25000)
    for _ in range(80):
        if observed:
            break
        page.wait_for_timeout(100)
    user = observed[-1] if observed else request(session, '/site/user_info')
    account = user.get('user_number') or user.get('account')
    if not account:
        raise ServiceError('没有取得当前账号标识。', code='authentication_required')
    session.confirm(account)


def check():
    with browser_session('portal') as s:
        connected(s)
        return {'connected': True, 'read_only': True}


def services(keyword='', limit=30):
    if not isinstance(keyword, str) or len(keyword) > 100 or not 1 <= limit <= 100:
        raise ServiceError('关键词最长 100 字，数量须为 1–100。', code='invalid_arguments')
    with browser_session('portal') as s:
        connected(s)
        data = request(s, '/site/app_recommend_list', cate_id=5, keyword=keyword, terminal=4)
        if not isinstance(data, list):
            raise ServiceError('服务目录结构变化。', code='unavailable')
        items = []
        for x in data:
            item = {'id': str(x['id']), 'name': plain(x.get('name')), 'description': plain(x.get('guide_desc')),
                    'departments': [plain(d.get('name')) for d in x.get('relevance_depart', [])],
                    'url': public_link(x.get('pc_url') or '', s.base) if x.get('pc_url') else None}
            if not keyword or keyword.casefold() in (item['name'] + item['description'] + ' '.join(item['departments'])).casefold():
                items.append(item)
        return {'items': items[:limit], 'returned': min(len(items), limit), 'matching_count': len(items),
                'truncated': len(items) > limit, 'scope': '当前账号首页服务目录', 'source_url': s.base + '/page/site/index', 'untrusted_content': True}


def progress(kind='todos', page=1, limit=20, keyword=''):
    if kind not in {'todos', 'applications'} or not 1 <= page <= 1000 or not 1 <= limit <= 50 or len(keyword) > 100:
        raise ServiceError('进度查询参数无效。', code='invalid_arguments')
    with browser_session('portal') as s:
        connected(s)
        if kind == 'todos':
            data = request(s, '/site/get_my_daiban_list', keyword=keyword, status=0, app_id='', page=page, pageSize=limit)
        else:
            data = request(s, '/site/get_officeflow_inst', keyword=keyword, status=0, p=page, page_size=limit, start_time='', end_time='')
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise ServiceError('办理进度结构变化。', code='unavailable')
        # Explicit presentation fields only; never copy arbitrary internal user objects.
        fields = ('id', 'title', 'name', 'app_name', 'flow_name', 'status', 'status_name', 'create_time', 'update_time', 'start_time', 'node_name')
        items = [{k: plain(x[k]) for k in fields if k in x} for x in data['data'][:limit]]
        total = int(data['total'])
        return {'kind': kind, 'items': items, 'total': total, 'page': page,
                'next_page': page + 1 if total > page * int(data.get('perpage') or limit) else None,
                'source_url': s.base + '/page/site/index', 'untrusted_content': True,
                'note': '仅查询一网通办登记的流程；空列表不代表其他独立平台没有任务。'}
