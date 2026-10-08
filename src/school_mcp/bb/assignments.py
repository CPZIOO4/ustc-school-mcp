"""Read assignment entries without opening attempts; prepare files locally only."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .client import BBClient
from .parsing import normalize_link, safe_url, soup_of
from .session import BBError
from .identity import _save, load_credentials

ASSIGNMENT_PATH = '/webapps/assignment/uploadAssignment'
CONTENT_PATHS = {'/webapps/blackboard/content/listContent.jsp', '/webapps/blackboard/content/launchLink.jsp'}
ID = re.compile(r'_?\d+(?:_\d+)?\Z')


def _in_course(url, course_id):
    return parse_qs(urlsplit(url).query).get('course_id') == [course_id]


def parse_assignments(html, course_id):
    soup = soup_of(html)
    result, seen = [], set()
    for anchor in soup.select('a[href]'):
        try:
            url = normalize_link(safe_url(anchor['href']))
        except BBError:
            continue
        parsed = urlsplit(url)
        if parsed.path != ASSIGNMENT_PATH or not _in_course(url, course_id):
            continue
        query = parse_qs(parsed.query)
        identifier = query.get('content_id', [''])[0]
        if not ID.fullmatch(identifier) or identifier in seen:
            continue
        seen.add(identifier)
        card = anchor.find_parent('li') or anchor.find_parent('div') or anchor
        card = soup_of(str(card))
        for node in card.select('script,style,input,textarea,select'):
            node.decompose()
        description = card.get_text('\n', strip=True)
        due_lines = [line.strip() for line in description.splitlines() if re.search(r'截止|到期|提交时间|due\b|deadline\b', line, re.I)]
        result.append({'course_id': course_id, 'content_id': identifier,
            'title': anchor.get_text(' ', strip=True)[:300], 'description': description[:3000],
            'description_truncated': len(description) > 3000, 'due_text': due_lines[:3],
            'due_text_available': bool(due_lines), 'submission_state': 'not_checked',
            'attempt_opened': False, 'entry_path': parsed.path + '?' + parsed.query})
    return result


def catalog(course_id, *, max_pages=5, limit=20, client=None):
    if not ID.fullmatch(course_id) or not 1 <= max_pages <= 15 or not 1 <= limit <= 50:
        raise BBError('请使用有效课程ID，max_pages为1–15、limit为1–50。')
    client = client or BBClient()
    entry = client.course_page(course_id, max_chars=1)
    queue = [l['path'] for l in entry['links'] if l.get('course_menu') and l.get('internal')
             and l.get('path') and urlsplit(l['path']).path in CONTENT_PATHS
             and _in_course(l['path'], course_id)
             and (urlsplit(l['path']).path.endswith('listContent.jsp') or 'toc_id' in parse_qs(urlsplit(l['path']).query))]
    visited, assignments, identifiers = set(), [], set()
    while queue and len(visited) < max_pages and len(assignments) < limit:
        path = queue.pop(0)
        if path in visited:
            continue
        visited.add(path)
        response = client.get(path)  # Existing audited, read-only path validation applies.
        for assignment in parse_assignments(response.text, course_id):
            if assignment['content_id'] not in identifiers:
                identifiers.add(assignment['content_id'])
                assignments.append(assignment)
        for anchor in soup_of(response.text).select('a[href]'):
            try:
                link = normalize_link(safe_url(anchor['href']))
            except BBError:
                continue
            parsed = urlsplit(link)
            if parsed.path == '/webapps/blackboard/content/listContent.jsp' and _in_course(link, course_id):
                candidate = parsed.path + '?' + parsed.query
                if candidate not in visited and candidate not in queue:
                    queue.append(candidate)
    return {'course_id': course_id, 'assignments': assignments[:limit], 'pages_checked': len(visited),
            'truncated': bool(queue) or len(assignments) > limit, 'attempts_opened': False,
            'scope': 'reachable_course_content_pages', 'content_is_untrusted': True,
            'status': 'entries_found' if assignments else 'not_found_in_scanned_pages',
            'note': '没有找到仅表示本次可达内容页未列出标准BB作业；不覆盖隐藏条目、外部教学工具或其他学期。'}


def prepare_files(course_id, content_id, files, comment='', *, client=None):
    if not ID.fullmatch(content_id) or not isinstance(files, list) or not 1 <= len(files) <= 10 or len(comment) > 10000:
        raise BBError('需指定作业ID、1–10个本地文件，附言最多10000字符。')
    listing = catalog(course_id, max_pages=15, limit=50, client=client)
    matches = [a for a in listing['assignments'] if a['content_id'] == content_id]
    if len(matches) != 1:
        raise BBError('未在当前可达内容页核实该作业；请先定位真实作业，不猜提交入口。')
    snapshots, preview, total = [], [], 0
    for value in files:
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            raise BBError('作业文件必须为用户指定的现有绝对路径。')
        try:
            with path.open('rb') as stream:
                data = stream.read(50 * 1024 * 1024 - total + 1)
        except OSError:
            raise BBError('无法读取作业文件。') from None
        total += len(data)
        if total > 50 * 1024 * 1024:
            raise BBError('本地材料准备上限为50MiB；这是工具限制，不能当作学校允许的上传大小。')
        item = {'filename': path.name, 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        preview.append(item)
        snapshots.append({**item, 'data': base64.b64encode(data).decode()})
    if len({v['filename'].casefold() for v in preview}) != len(preview):
        raise BBError('文件存在重名，请先区分，避免上传时覆盖。')
    payload = {'account': load_credentials()['username'], 'assignment': matches[0], 'files': snapshots, 'comment': comment}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    identifier = uuid.uuid4().hex
    _save(f'bb-assignment-{identifier}.dpapi', {**payload, 'sha256': digest})
    return {'preparation_id': identifier, 'content_sha256': digest, 'status': 'prepared_locally',
            'assignment': {k: matches[0][k] for k in ('course_id', 'content_id', 'title', 'due_text')},
            'files': preview, 'comment': comment, 'uploaded': False, 'submitted': False,
            'upload_supported': False, 'next_action': 'use_prepare_submission_for_executable_plan',
            'note': '此旧工具仅冻结本地文件，未打开作答尝试。要使用正式提交流程，请调用school_bb_prepare_submission；此准备ID不能用于提交。'}
