"""Contracts for the observed Blackboard Original individual assignment form."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qs, urlsplit

from .assignment_review import parse_review
from .parsing import safe_url, soup_of
from .session import BBError

CHINA = timezone(timedelta(hours=8))


def require(condition, message='作业页面与已验证模板不一致，已停止。'):
    if not condition:
        raise BBError(message)


def deadline(text):
    match = re.search(r'(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日(?:\s*星期\S)?\s*(上午|下午)?\s*(\d{1,2})[:：](\d{2})', text)
    if not match:
        return None
    year, month, day, ampm, hour, minute = match.groups()
    hour = int(hour)
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == '下午' else 0)
    try:
        return datetime(int(year), int(month), int(day), hour, int(minute), tzinfo=CHINA).isoformat()
    except ValueError:
        return None


def validate_download(url, assignment, attempt_id=None):
    target = safe_url(url)
    p = urlsplit(target)
    q = parse_qs(p.query, keep_blank_values=True)
    require(p.path == '/webapps/assignment/download' and not p.fragment)
    require(set(q) <= {'course_id', 'attempt_id', 'file_id', 'fileName'}
            and q.get('course_id') == [assignment['course_id']])
    for field in ('attempt_id', 'file_id'):
        require(len(q.get(field, [])) == 1 and re.fullmatch(r'_\d+_\d+', q[field][0]))
    if attempt_id:
        require(q['attempt_id'] == [attempt_id])
    return target, q['attempt_id'][0]


def form_contract(html, assignment):
    s = soup_of(html)
    f = s.select_one('#uploadAssignmentFormId')
    require(f is not None, '尚未识别支持的个人作业上传表单。')
    target = safe_url(f.get('action', ''))
    p = urlsplit(target)
    require(p.path == '/webapps/assignment/uploadAssignment' and parse_qs(p.query) == {'action': ['submit']})
    require(f.get('method', '').lower() == 'post' and f.get('enctype') == 'multipart/form-data')
    def values(name):
        return [n.get('value', '') for n in f.find_all('input', attrs={'name': name})]
    for key in ('course_id', 'content_id'):
        require(values(key) == [assignment[key]], '提交表单目标与准备记录不一致。')
    require(values('attempt_id') == [''] and values('remove_file_id') == [''], '发现已有草稿/附件操作，不自动续写或替换。')
    for n in f.select('input[name]'):
        require(not ('group' in n['name'].lower() and n.get('value')), '小组作业尚未适配。')
    require(f.select_one('#newFile_chooseLocalFile') is not None)
    require(f.select_one('input[name="bottom_提交"][type="submit"]') is not None)
    for area in f.select('textarea'):
        require(not area.get_text(strip=True), '表单已有文本或附言，不能覆盖；请先核实既有草稿。')
    require(not f.select('#newFile_table a.attachment'), '发现已有附件，不能自动覆盖。')
    return target


def snapshot(html, assignment):
    s = soup_of(html)
    review = parse_review(html, include_attachment_names=True)
    if review['page_kind'] == 'submission_history':
        require(not review['continue_control_present'], '发现继续作答/草稿入口，当前工具不自动续写。')
        files, attempts = [], set()
        for row in s.select('#currentAttempt_submissionList li'):
            name = row.select_one('a.attachment')
            link = row.select_one('a.dwnldBtn[href]')
            require(name is not None and link is not None)
            url, attempt_id = validate_download(link['href'], assignment)
            attempts.add(attempt_id)
            files.append({'filename': name.get_text(' ', strip=True), 'url': url})
        require(files and len(attempts) == 1, '历史尝试缺少唯一可核验的附件记录，已停止。')
        button = s.select_one('input[value="开始新的"], input[value="Start New"]')
        new_url = None
        if button:
            match = re.fullmatch(r"document.location='([^']+)';", button.get('onclick', ''))
            require(match is not None)
            new_url = safe_url(match[1]); q = parse_qs(urlsplit(new_url).query)
            require(urlsplit(new_url).path == '/webapps/assignment/uploadAssignment'
                    and q == {'action': ['newAttempt'], 'course_id': [assignment['course_id']], 'content_id': [assignment['content_id']]})
        due = ' '.join(review['due_text'])
        return {'kind': 'history', 'attempt_id': next(iter(attempts)), 'files': files,
                'new_url': new_url, 'due_text': due, 'due_at': deadline(due),
                'attempt_time_text': review['attempt_time_text'], 'late': review['late_label_present']}
    form_contract(html, assignment)
    # A recognized pristine upload form is different from a saved attempt.
    for n in s.select('script,style,input,textarea,select'):
        n.decompose()
    text = (s.select_one('#contentPanel') or s).get_text(' ', strip=True)
    match = re.search(r'截止日期\s*(.{0,90})', text)
    due = match[1].split('满分')[0].strip() if match else ''
    return {'kind': 'new_form', 'attempt_id': None, 'files': [], 'new_url': None,
            'due_text': due, 'due_at': deadline(due), 'attempt_time_text': None, 'late': False}


def file_record(name, data):
    require(bool(name) and len(name) <= 125 and name not in {'.', '..'}
            and not any(c in name for c in '/\\\x00\r\n'), '文件名不符合已验证上传要求。')
    require(bool(data), '不提交空文件。')
    return {'filename': name, 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def baseline(snapshot):
    return {k: snapshot[k] for k in ('kind', 'attempt_id', 'files', 'due_text', 'due_at')}
