"""Fixed BB/mail/JW workflows. Page and message contents never become instructions."""
from __future__ import annotations

from datetime import timedelta
import re
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from .workflow_store import require
from . import script_jobs as jobs


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Schedule(StrictModel):
    start_at: str = Field(description='带时区的ISO时间；立即执行也须填写当前时间')
    end_at: str = Field(description='明确停止时间；最长30天，电脑须保持开机')
    poll_seconds: int = Field(default=60, ge=60, le=3600, strict=True)


class MailSend(StrictModel):
    kind: Literal['mail_send']
    draft_id: str = Field(pattern='^[a-f0-9]{32}$')
    content_sha256: str = Field(pattern='^[a-f0-9]{64}$')
    save_sent_copy: bool = False


class MailScan(StrictModel):
    kind: Literal['mail_watch', 'mail_archive']
    mailbox: str = Field(default='INBOX', min_length=1, max_length=512)
    sender: str = Field(default='', max_length=254)
    subject: str = Field(default='', max_length=200)
    since: str = Field(default='', description='YYYY-MM-DD；包含已有邮件时必须设置')
    before: str = ''
    include_existing: bool = False
    batch_size: int = Field(default=10, ge=1, le=25, strict=True)
    max_messages: int = Field(default=100, ge=1, le=1000, strict=True)


class MailReplies(StrictModel):
    kind: Literal['mail_replies']
    draft_id: str = Field(pattern='^[a-f0-9]{32}$')
    mailbox: str = Field(default='INBOX', min_length=1, max_length=512)


MailRequest = Union[MailSend, MailScan, MailReplies]


class BBSubmit(StrictModel):
    preparation_id: str = Field(pattern='^[a-f0-9]{32}$')
    expected_sha256: str = Field(pattern='^[a-f0-9]{64}$')


class JWWatch(StrictModel):
    semester_id: int = Field(gt=0, strict=True)
    keyword: str = Field(min_length=1, max_length=100)
    lesson_id: int = Field(gt=0, strict=True, description='来自官方开课结果的课堂ID，唯一绑定监控目标')
    minimum_seats: int = Field(default=1, ge=1, strict=True)


def mail_client():
    from .mail.client import MailClient
    from .mail.config import load_config
    return MailClient(load_config())


def mail_boundary(client, mailbox):
    from .mail.client import _ok
    from .mail.parsing import quote, encode_mailbox
    with client.session() as connection:
        validity, _ = client.select(connection, mailbox)
        _, raw = connection.response('UIDNEXT')
        try:
            next_uid = int(raw[0])
            if next_uid <= 0:
                raise ValueError
        except (ValueError, TypeError, IndexError):
            # Coremail may omit UIDNEXT in EXAMINE; STATUS is a read-only standard fallback.
            data = _ok(*connection.status(quote(encode_mailbox(mailbox)), '(UIDVALIDITY UIDNEXT)'), '查询邮件监控边界')
            values = b' '.join(v for v in data if isinstance(v, bytes))
            current = re.search(rb'\bUIDVALIDITY (\d+)\b', values)
            following = re.search(rb'\bUIDNEXT (\d+)\b', values)
            require(current and int(current[1]) == validity, 'UIDVALIDITY 改变，停止建立监控。')
            if following and int(following[1]) > 0:
                next_uid = int(following[1])
            else:
                # Some installations omit it even in STATUS. Capture the largest extant
                # UID with no headers/body; UID monotonicity makes this a valid boundary.
                data = _ok(*connection.uid('SEARCH', 'ALL'), '建立新邮件UID边界')
                identifiers = [int(v) for v in (data[0] or b'').split()]
                require(all(v > 0 for v in identifiers), '邮箱返回无效UID。')
                next_uid = max(identifiers, default=0) + 1
    return validity, next_uid - 1


def scan_uids(client, parameters, checkpoint):
    """Ascending UID cursor, not an offset that shifts after moving a previous batch."""
    from .mail.client import _ok
    criteria = client._query(unread_only=False, sender=parameters['sender'], subject=parameters['subject'],
                             text='', since=parameters['since'], before=parameters['before'])
    cursor = checkpoint.get('cursor', parameters['initial_cursor'])
    criteria += ['UID', f'{cursor + 1}:*']
    args = ['CHARSET', 'UTF-8', *[v.encode('utf-8') for v in criteria]] if any(not v.isascii() for v in criteria) else criteria
    with client.session() as connection:
        validity, _ = client.select(connection, parameters['mailbox'], expected=parameters['uid_validity'])
        data = _ok(*connection.uid('SEARCH', *args), '查询脚本限定邮件')
        identifiers = sorted({int(v) for v in (data[0] or b'').split() if int(v) > cursor})
    remaining = parameters['max_messages'] - checkpoint.get('processed', 0)
    selected = identifiers[:min(parameters['batch_size'], remaining)]
    return [dict(uid=uid, uid_validity=validity, mailbox=parameters['mailbox']) for uid in selected], len(identifiers) > len(selected)


def schedule_mail(request: MailRequest, schedule: Schedule, authorized=False):
    from .mail import outbox, classification
    client = mail_client()
    parameters = request.model_dump()
    if request.kind in {'mail_send', 'mail_archive'}:
        require(authorized is True, '先取得用户对固定邮件或限定范围规则归档的授权。')
    if isinstance(request, (MailSend, MailReplies)):
        state, payload = outbox.Outbox().get(request.draft_id)
        require(payload['account'] == client.config.address, '发件记录与当前账号不符。')
        if isinstance(request, MailSend):
            require(state == 'ready' and payload['sha256'] == request.content_sha256, '草稿不处于可发送状态或摘要不符。')
        else:
            require(state in {'accepted', 'partial', 'unknown', 'sending'}, '只跟踪已有发送尝试的回复。')
    else:
        client._query(unread_only=False, sender=request.sender, subject=request.subject, text='', since=request.since, before=request.before)
        require(not request.include_existing or bool(request.since), '处理已有邮件须给出 since 日期，不能隐式扫描全部历史。')
        validity, cursor = mail_boundary(client, request.mailbox)
        parameters.update(uid_validity=validity, initial_cursor=0 if request.include_existing else cursor)
        if request.kind == 'mail_archive':
            config = classification.get_rules(client)
            require(config['configured'], '先确认归档类别并保存规则。')
            parameters['rules_revision'] = config['revision']
    return create('mail', request.kind, parameters, schedule)


def schedule_bb(request: BBSubmit, schedule: Schedule, authorized=False):
    from .bb.submissions import Store, submission_status
    require(authorized is True, '先取得用户对该作业、冻结材料及提交时间的授权。')
    status = submission_status(request.preparation_id)
    require(status['can_execute'] and status['content_sha256'] == request.expected_sha256, '作业准备状态或材料摘要不匹配。')
    plan = Store().get(request.preparation_id)['plan']
    require(jobs.timestamp(schedule.end_at) < jobs.timestamp(plan['prepared_at']) + timedelta(hours=24), '作业准备有效期24小时，任务结束时间须在有效期内。')
    require(plan['allow_late'] or jobs.timestamp(schedule.end_at) <= jobs.timestamp(plan['baseline']['due_at']), '任务结束时间超出作业截止；不能隐式允许迟交。')
    return create('bb', 'bb_submit', request.model_dump(), schedule)


def schedule_jw(request: JWWatch, schedule: Schedule):
    from .jw.academic import AcademicClient
    found = find_offering(AcademicClient(), request.semester_id, request.keyword, request.lesson_id)
    require(found is not None, '未在完整查询中找到该课堂，请缩小关键词或重新核对ID。')
    return create('jw', 'jw_watch', request.model_dump(), schedule)


def create(service, kind, parameters, schedule):
    identifier = jobs.JobStore().create(service, kind, parameters, schedule.start_at, schedule.end_at,
                                       schedule.poll_seconds, jobs.account(service))
    jobs.launch(identifier)
    return jobs.view(identifier, service)


def find_offering(client, semester_id, keyword, lesson_id):
    matches = []
    page = 1
    for _ in range(5):
        result = client.offerings(semester_id, keyword, page=page, limit=50)
        require(not result['truncated'], '开课查询被截断，请缩小关键词。')
        matches.extend(r for r in result['offerings'] if str(r['lesson_id']) == str(lesson_id))
        page = result['next_page']
        if not page:
            require(len(matches) <= 1, '课堂ID重复，不能确定目标。')
            return matches[0] if matches else None
    raise ValueError('开课超过5页，请缩小关键词。')


def plan_timetable(semester_id, keywords, constraints, client=None):
    from .jw.academic import AcademicClient
    from .jw.planner import Candidate, generate
    require(type(semester_id) is int and semester_id > 0 and 1 <= len(keywords) <= 5
            and all(isinstance(k, str) and 1 <= len(k.strip()) <= 100 for k in keywords), '使用实际学期ID和1–5个课程关键词。')
    client = client or AcademicClient()
    context = client.planning_context(semester_id)
    if context['state'] != 'context_ready':
        return context
    by_id = {c['lesson_id']: c for c in context['candidates']}
    for keyword in dict.fromkeys(keywords):
        page = 1
        for _ in range(5):
            result = client.offerings(semester_id, keyword, page=page, limit=50)
            if result['truncated']:
                return dict(state='needs_input', next_action='narrow_keywords', plans=[], reason='truncated_page')
            for row in result['offerings']:
                candidate = row['planner_candidate']
                if candidate is None:
                    return dict(state='needs_input', next_action='verify_missing_metadata', plans=[])
                by_id.setdefault(candidate['lesson_id'], candidate)
            if len(by_id) > 25:
                return dict(state='needs_input', next_action='narrow_keywords', plans=[], reason='more_than_25_candidates')
            page = result['next_page']
            if not page:
                break
        if page:
            return dict(state='needs_input', next_action='narrow_keywords', plans=[], reason='more_than_5_pages')
    result = generate([Candidate.model_validate(c) for c in by_id.values()], constraints)
    return {**result, 'semester_id': semester_id, 'existing_courses_preserved': True,
            'mutation_performed': False, 'content_is_untrusted': True}


def prepare_bb_by_name(course_keyword, assignment_keyword, files=None, term='', comment='',
                       resubmit=False, reuse_previous_files=False, allow_late=False):
    from .bb.client import BBClient
    from .bb.assignments import catalog
    from .bb.submissions import prepare_submission
    require(bool(course_keyword.strip()) and bool(assignment_keyword.strip()) and len(assignment_keyword) <= 200, '提供课程和作业关键词。')
    courses = BBClient().courses(course_keyword, term)['courses']
    if len(courses) != 1:
        return dict(state='needs_input', next_action='specify_unique_course', courses=courses, content_is_untrusted=True)
    course_id = courses[0]['course_id']
    listing = catalog(course_id, max_pages=15, limit=50)
    candidates = [a for a in listing['assignments'] if assignment_keyword.casefold() in a['title'].casefold()]
    if listing.get('truncated') or len(candidates) != 1:
        return dict(state='needs_input', next_action='specify_assignment_id', truncated=listing.get('truncated', False),
                    assignments=[{k: a[k] for k in ('course_id', 'content_id', 'title')} for a in candidates], content_is_untrusted=True)
    return prepare_submission(course_id, candidates[0]['content_id'], files, comment,
                              reuse_previous_files=reuse_previous_files, resubmit=resubmit, allow_late=allow_late)


def step(identifier, job, store):
    p, checkpoint = job['parameters'], dict(job['checkpoint'])
    kind = job['kind']
    if kind == 'bb_submit':
        from .bb.submissions import submit_assignment, submission_status
        checkpoint['operation'] = {'preparation_id': p['preparation_id'], 'status_tool': 'school_bb_submission_status'}
        store.effect(identifier, checkpoint)
        result = submit_assignment(p['preparation_id'], p['expected_sha256'])
        if result['state'] == 'uncertain':
            result = submission_status(p['preparation_id'], verify=True)
        return ('completed' if result['state'] == 'verified' else 'review',
                dict(outcome=result['state'], receipt=result), checkpoint)
    if kind == 'jw_watch':
        from .jw.academic import AcademicClient
        client = AcademicClient()
        window = client.window()
        row = find_offering(client, p['semester_id'], p['keyword'], p['lesson_id'])
        require(row is not None, '监控课堂已消失，停止。')
        capacity, selected = row.get('capacity'), row.get('selected_count')
        seats = capacity - selected if type(capacity) is int and type(selected) is int and 0 <= selected <= capacity else None
        result = dict(outcome='monitoring', window=window['state'], available_seats=seats,
                      lesson_id=p['lesson_id'], can_execute=False, mutation_performed=False)
        if window['state'] != 'window_closed':
            if seats is not None and seats < p['minimum_seats']:
                result['outcome'] = 'waiting_for_seats'
                return 'waiting', result, checkpoint
            result['outcome'] = 'live_selection_contract_not_verified'
            result['requested_seats_available'] = seats is not None and seats >= p['minimum_seats']
            if seats is None:
                result['reason_code'] = 'capacity_not_provided'
            return 'review', result, checkpoint
        return 'waiting', result, checkpoint
    client = mail_client()
    from .mail import outbox, sent, classification, actions
    if kind in {'mail_send', 'mail_replies'}:
        state, payload = outbox.Outbox().get(p['draft_id'])
        require(payload['account'] == client.config.address, '邮箱账号已变化。')
        if kind == 'mail_send':
            checkpoint['operation'] = {'draft_id': p['draft_id'], 'status_tool': 'school_mail_send_status'}
            store.effect(identifier, checkpoint)
            result = outbox.send(client.config, p['draft_id'], p['content_sha256'])
            if result['status'] not in {'accepted', 'partial'}:
                return 'review', dict(outcome=result['status'], receipt=result), checkpoint
            copy = sent.save(client, p['draft_id']) if p['save_sent_copy'] else None
            success = result['status'] == 'accepted' and (copy is None or copy['status'] in {'present', 'saved'})
            return ('completed' if success else 'review', dict(outcome='server_accepted' if success else 'partial_or_copy_requires_review',
                    receipt=result, sent_copy=copy, delivery_confirmed=False), checkpoint)
        require(state in {'accepted', 'partial', 'unknown', 'sending'}, '发件记录状态已变化。')
        result = client.find_replies(payload['message_id'], p['mailbox'], limit=50)
        # Do not retain subjects, authors, or possible OTPs in background-job receipts.
        refs = [{k: m[k] for k in ('uid', 'uid_validity', 'mailbox')} for m in result['messages']]
        return ('completed' if refs else 'review' if result['truncated'] else 'waiting',
                dict(outcome=result['status'], messages=refs, truncated=result['truncated'],
                     match_basis=result['match_basis']), checkpoint)
    require(kind in {'mail_watch', 'mail_archive'}, '未知脚本流程。')
    if kind == 'mail_archive':
        require(classification.get_rules(client).get('revision') == p['rules_revision'], '归档规则已改变，重新核对任务范围。')
    refs, backlog = scan_uids(client, p, checkpoint)
    counts = dict(checkpoint.get('classification_counts', {}))
    if refs and kind == 'mail_archive':
        preview = classification.preview(client, refs, max_chars=0)
        for item in preview['items']:
            decision = item['decision']
            counts[decision] = counts.get(decision, 0) + 1
        plan = classification.prepare(client, preview['preview_id'], preview['preview_sha256'])
        if plan['status'] != 'no_changes':
            checkpoint['operation'] = {'plan_id': plan['plan_id'], 'status_tool': 'school_mail_action_status'}
            require(classification.get_rules(client).get('revision') == p['rules_revision'], '归档规则已改变。')
            store.effect(identifier, checkpoint)
            receipt = actions.execute(client, plan['plan_id'], plan['plan_sha256'])
            if receipt['status'] != 'completed':
                return 'review', dict(outcome='archive_requires_review', receipt=receipt), checkpoint
    if refs:
        checkpoint.update(cursor=refs[-1]['uid'], processed=checkpoint.get('processed', 0) + len(refs), latest_batch=refs)
    checkpoint['classification_counts'] = counts
    done = checkpoint.get('processed', 0) >= p['max_messages']
    return ('completed' if done else 'waiting',
            dict(outcome='message_limit_reached' if done else 'monitoring',
                 processed=checkpoint.get('processed', 0), latest_batch=checkpoint.get('latest_batch', []), backlog=backlog,
                 classification_counts=counts), checkpoint)
