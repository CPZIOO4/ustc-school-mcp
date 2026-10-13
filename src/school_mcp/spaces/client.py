"""Library study-space inventory, availability and reservation preparation."""
from datetime import datetime
from ..campus.session import browser_session
from ..service_errors import ServiceError


def request(s, path, params=None):
    data = s.get(path, params).json()
    if data.get('code') != 0:
        raise ServiceError('空间预约平台未确认查询成功。', code='authentication_required' if data.get('code') == 300 else 'unavailable')
    return data


def connected(s):
    observed = []
    def capture(r):
        if r.url.split('?')[0] == s.base + '/ic-web/auth/userInfo':
            try:
                x = r.json()
                if x.get('code') == 0 and isinstance(x.get('data'), dict):
                    observed.append(x['data'])
            except ValueError:
                pass
    s.context.on('response', capture)
    s.goto()
    for _ in range(150):
        if observed:
            break
        s.page.wait_for_timeout(100)
    s.require_site()
    user = observed[-1] if observed else request(s, '/ic-web/auth/userInfo')['data']
    s.confirm(user.get('logonName') or user.get('pid'))
    return user


def check():
    with browser_session('spaces') as s:
        connected(s)
        return {'connected': True, 'campus': '西区图书馆', 'scope': 'study_spaces'}


def categories():
    with browser_session('spaces') as s:
        connected(s)
        items = request(s, '/ic-web/roomMenu')['data']
        return {'campus': '西区图书馆', 'items': [{k:x.get(k) for k in ('kindId', 'kindName', 'resvMemo')} for x in items],
                'untrusted_content': True}


def availability(date, kind_id, page=1, limit=20):
    try:
        value = datetime.strptime(date, '%Y-%m-%d').strftime('%Y%m%d')
        if not isinstance(kind_id, int) or kind_id < 1 or not 1 <= page <= 100 or not 1 <= limit <= 50:
            raise ValueError()
    except (ValueError, TypeError):
        raise ServiceError('请提供 YYYY-MM-DD 日期、分类 id 和有效页码。', code='invalid_arguments') from None
    with browser_session('spaces') as s:
        connected(s)
        known = request(s, '/ic-web/roomMenu')['data']
        if kind_id not in {x['kindId'] for x in known}:
            raise ServiceError('请使用当前分类列表返回的 kindId。', code='invalid_arguments')
        data = request(s, '/ic-web/reserve', {'sysKind': 1, 'resvDates': value, 'page': page, 'pageSize': limit, 'kindIds': kind_id, 'labId': ''})
        items = []
        for x in data['data']:
            # Other reservation holders' names, IDs and contact data are omitted.
            item = {k:x.get(k) for k in ('devId','devName','kindId','kindName','labName','minUser','maxUser','openStart','openEnd','openTimes','openState','devStatus','onlyView')}
            item['occupied'] = [{k:r.get(k) for k in ('startTime','endTime','resvBeginTime','resvEndTime')} for r in x.get('resvInfo', [])]
            item['rules'] = {k:x.get('resvRule', {}).get(k) for k in ('minResvTime','maxResvTime','earliestResvTime','latestResvTime','cancelTime','timeInterval')}
            items.append(item)
        total = int(data.get('count') or 0)
        return {'date': date, 'campus': '西区图书馆', 'items': items, 'total': total, 'page': page,
                'next_page': page + 1 if page * limit < total else None, 'untrusted_content': True,
                'note': '可用性只代表查询时刻。须同时核对人数、开放时段、预约规则；查询不占位。'}
