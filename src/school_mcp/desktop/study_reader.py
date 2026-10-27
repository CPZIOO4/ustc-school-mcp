"""Fixed read-only adapters; Rust owns workspace state and personal task changes."""
from urllib.parse import urlsplit, parse_qs
import time
from ..bb.client import BBClient
from ..safety_log import record
from ..bb.assignments import catalog, ID
from ..bb.assignment_review import inspect_assignment
from ..bb.identity import load_credentials
from ..bb.session import BBError

HISTORY_ERRORS = {
    'upstream_error': 'BB 原网站返回了错误页面。已保留上次核实记录；请稍后重试，持续出现时联系学校平台管理员。',
    'timeout': 'BB 作业历史页响应超时。已保留上次核实记录；可稍后点击“核实提交记录”重试，或打开原作业页面。',
    'network_unavailable': '暂时无法连接 BB 作业历史页。已保留上次核实记录；请检查网络后重试。',
    'cooldown': 'BB 查询正在等待限速冷却。已保留上次核实记录；请稍后重试。',
}


def read(value, stopped, notify):
    account = load_credentials()['username']
    client = BBClient()
    action = value.get('scope')
    def check():
        if stopped.is_set():
            raise ValueError('读取已停止，已保存的内容保留。')
        if load_credentials()['username'] != account:
            raise ValueError('账号已变化，停止读取。')
    check()
    def progress(stage, detail):
        check()
        notify(stage, detail)
    if action == 'courses':
        result = client.courses(term=value.get('term', ''))
    else:
        cid = value.get('course_id', '')
        if not isinstance(cid, str) or not ID.fullmatch(cid):
            raise ValueError('课程编号无效。')
        if action == 'assignment':
            started = time.monotonic()
            try:
                result = {'verification': inspect_assignment(cid, value['content_id'], include_attachment_names=True, progress=progress)}
            except BBError as exc:
                if exc.code not in HISTORY_ERRORS:
                    raise
                elapsed = round((time.monotonic() - started) * 1000)
                incident = record(code=exc.code, service='bb', stage='tool', outcome='read_failed', exc=exc, duration_ms=elapsed)
                result = {'course_id': cid, 'content_id': value['content_id'],
                          'verification_error': {'code': exc.code, 'message': HISTORY_ERRORS[exc.code],
                                                 'duration_ms': elapsed, 'error_id': incident.get('error_id')}}
                notify('study_read', HISTORY_ERRORS[exc.code])
        elif action == 'page':
            path = value['path']
            if parse_qs(urlsplit(path).query).get('course_id') != [cid]:
                raise ValueError('材料目录不属于这门课程。')
            if urlsplit(path).path != '/webapps/blackboard/content/listContent.jsp':
                raise ValueError('该材料请在原网站中打开。')
            result = {'course_id': cid, 'material_pages': [client.read_page(path, max_chars=24000)]}
        elif action == 'course':
            result = {'course_id': cid, 'errors': {}}
            for name, label, call in [
                ('page', '正在读取课程主页…', lambda: client.course_page(cid, max_chars=12000)),
                ('catalog', '正在读取作业目录…', lambda: catalog(cid, max_pages=15, limit=50, client=client)),
                ('announcements', '正在读取课程通知…', lambda: client.announcements(cid, max_chars=24000)),
            ]:
                check(); notify('study_read', label)
                try:
                    result[name] = call()
                except Exception as exc:
                    record(code='page_changed', service='bb', stage='tool', outcome='read_failed', exc=exc)
                    # Keep the previous successful section; no raw network details in UI.
                    result['errors'][name] = '本部分读取未完成，保留上次内容；可稍后重试。'
            if len(result['errors']) == 3:
                raise ValueError('课程读取未完成，请检查 BB 连接后重试。')
        else:
            raise ValueError('不支持的读取范围。')
    check()
    return result
