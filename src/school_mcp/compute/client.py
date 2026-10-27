"""Fixed read operations on 107 SCOW. No submission or shell execution."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, parse_qs

from ..campus.session import browser_session
from ..service_errors import ServiceError

BASE = 'https://107.ustc.edu.cn'
CST = timezone(timedelta(hours=8))
FIELDS = ('jobId', 'name', 'account', 'partition', 'qos', 'state', 'reason',
          'elapsed', 'timeLimit', 'submitTime', 'startTime', 'endTime',
          'nodes', 'cores', 'gpus', 'cpusAlloc', 'nodesAlloc', 'gpusAlloc',
          'memReq', 'memAlloc', 'nodelist')


def check_owner(url, account):
    value = urlsplit(url)
    users = parse_qs(value.query).get('userId', [])
    if value.scheme != 'https' or value.hostname != '107.ustc.edu.cn' or value.path != '/api/job/getRunningJobs' or len(users) != 1 or users[0].casefold() != account.casefold():
        raise ServiceError('107 当前账号与学校账号不一致，已停止读取。', code='identity_mismatch')


@contextmanager
def connected():
    with browser_session('compute') as s:
        observed = []
        def response(r):
            if urlsplit(r.url).path == '/api/job/getRunningJobs':
                observed.append(r)
        s.page.on('response', response)
        s.goto('/jobs/runningJobs')
        login = s.page.get_by_text('统一认证登录', exact=True)
        if login.count():
            login.click()
            try:
                s.page.wait_for_url(BASE + '/dashboard', timeout=25000)
                # URL changes before SCOW finishes loading the authenticated app.
                # Wait for its navigation before leaving the CAS callback page.
                s.page.get_by_text('登录集群', exact=True).wait_for(timeout=25000)
            except Exception:
                raise ServiceError('107 统一认证尚未完成，请在账号页恢复统一身份后重试。', code='authentication_required') from None
            s.goto('/jobs/runningJobs')
        for _ in range(150):
            if observed:
                break
            s.page.wait_for_timeout(200)
        if not observed:
            raise ServiceError('107 未返回本人作业查询，当前结果无法确认。', code='unavailable')
        r = observed[-1]
        check_owner(r.url, s.account)
        if r.status != 200:
            raise ServiceError('107 作业查询暂时不可用。', code='unavailable')
        payload = r.json()
        if not isinstance(payload.get('results'), list):
            raise ServiceError('107 返回格式发生变化。', code='unavailable')
        s.confirm()
        s.running_jobs = payload['results']
        yield s


def check():
    with connected() as s:
        return {'connected': True, 'account': s.account, 'cluster': 'training',
                'running_count': len(s.running_jobs), 'source_url': BASE + '/jobs/runningJobs'}


def overview():
    with connected() as s:
        result = s.get('/api/dashboard/getAllSummaryClustersInfo',
                       {'clusterIds': 'training', 'isFullDisplayMode': 'true'}).json()
        if not isinstance(result.get('results'), list) or not result['results']:
            raise ServiceError('107 未返回资源概览。', code='unavailable')
        return {'account': s.account, 'checked_at': datetime.now(CST).isoformat(),
                'clusters': result['results'], 'source_url': BASE + '/dashboard',
                'scope': '共享集群实时状态；个人可申请额度以平台提交页为准。'}


def date_range(days):
    if type(days) is not int or not 1 <= days <= 31:
        raise ServiceError('查询时间范围为 1–31 天。', code='invalid_arguments')
    today = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days - 1)
    end = today + timedelta(days=1) - timedelta(milliseconds=1)
    return start.isoformat(), end.isoformat()


def clean_job(row):
    if not isinstance(row, dict) or type(row.get('jobId')) is not int:
        raise ServiceError('107 作业记录格式无效。', code='unavailable')
    result = {k: row[k] for k in FIELDS if k in row}
    result['source_url'] = BASE + '/jobs/allJobs'
    return result


def jobs(days=7, limit=10, job_id=None, running_only=False):
    start, end = date_range(days)
    if type(limit) is not int or not 1 <= limit <= 50 or (job_id is not None and (type(job_id) is not int or job_id <= 0)) or type(running_only) is not bool:
        raise ServiceError('请核对作业编号和查询数量。', code='invalid_arguments')
    with connected() as s:
        if running_only:
            rows = s.running_jobs
        else:
            raw = s.get('/api/job/getAllJobs', {'cluster': 'training', 'startTime': start, 'endTime': end}).json()
            rows = raw.get('results')
        if not isinstance(rows, list):
            raise ServiceError('107 未返回有效作业列表。', code='unavailable')
        rows = [clean_job(r) for r in rows]
        if job_id is not None:
            rows = [r for r in rows if r['jobId'] == job_id]
        rows.sort(key=lambda r: (r.get('submitTime', ''), r['jobId']), reverse=True)
        return {'account': s.account, 'checked_at': datetime.now(CST).isoformat(),
                'jobs': rows[:limit], 'total': len(rows), 'truncated': len(rows) > limit,
                'range': {'start': start, 'end': end}, 'running_only': running_only,
                'source_url': BASE + ('/jobs/runningJobs' if running_only else '/jobs/allJobs')}
