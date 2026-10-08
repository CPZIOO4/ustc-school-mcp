"""Deterministic monitor. No model, chat, or desktop automation is used."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from .registration import (Jobs, RegistrationBrowser, check_candidate, load_session,
                           require, school_time, stamp, summary, YoungError, TERMINAL,matches_name)
from .. import network


def monitoring_decision(detail, job, now):
    if job.get('expires_at') and now>stamp(job['expires_at']):return 'expired'
    if detail is None:return 'waiting_for_project'
    require(matches_name(detail,job),'活动名称不符合授权筛选条件。')
    require(not job.get('item_id') or detail.get('id')==job['item_id'],'活动标识不一致。')
    require(school_time(detail.get('applySt'))==stamp(job['opens_at']),'报名开始时间不一致。')
    if str(detail.get('booleanRegistration'))=='1':return 'already_registered'
    if now>school_time(detail.get('applyEt')):return 'expired'
    if now<stamp(job['opens_at']) or str(detail.get('applyStatus'))!='26':return 'waiting_for_open'
    return check_candidate(detail,job,now)


def monitor_job(job_id, *, sleep=time.sleep, clock=lambda:datetime.now(timezone.utc)):
    session=load_session();store=Jobs(session['account'])
    if not store.claim_monitor(job_id,clock()):return store.view(job_id)
    job=store.view(job_id);failures=0
    try:
        with RegistrationBrowser(session).open() as browser:
            while store.view(job_id)['state']=='monitoring':
                now=clock()
                if job.get('expires_at') and now>stamp(job['expires_at']):
                    store.transition(job_id,'monitoring','expired');break
                try:
                    browser.error=None
                    browser.policy_error=None
                    detail=browser.locate(job,allow_missing=True)
                    if detail is not None:
                        job['item_id']=detail['id']
                        job['resolved_name']=detail['itemName']
                    decision=monitoring_decision(detail,job,clock())
                    if decision=='eligible' and not browser.registration_button_ready():decision='waiting_for_button'
                    failures=0
                except network.CooldownError as exc:
                    store.progress(job_id,dict(phase='cooldown',retry_after_seconds=exc.retry_after_seconds,checked_at=clock().isoformat()))
                    wait_cancellable(store,job_id,exc.retry_after_seconds,sleep)
                    continue
                except YoungError:
                    # Wrong identity, target, unsupported fields or changed business rules are not transient.
                    raise
                except Exception:
                    failures+=1
                    require(failures<=3,'连续读取失败，需要检查网络或登录；未提交报名。')
                    delay=max(30*2**(failures-1),network.limiter.failure('young'))
                    store.progress(job_id,dict(phase='read_backoff',retry_after_seconds=delay,checked_at=clock().isoformat()))
                    wait_cancellable(store,job_id,delay,sleep)
                    continue
                store.progress(job_id,dict(phase=decision,checked_at=clock().isoformat(),item_id=detail.get('id') if detail else None))
                if decision=='expired':
                    store.transition(job_id,'monitoring','expired');break
                if decision=='already_registered':
                    store.transition(job_id,'monitoring','verified',dict(already_registered=True,item=summary(detail)));break
                if decision=='eligible':
                    def before_write():
                        # Durable state transition is the cross-process write gate.
                        check_candidate(detail,job,clock())
                        require(not job.get('expires_at') or clock()<=stamp(job['expires_at']),'监控截止时间已过。')
                        store.transition(job_id,'monitoring','submitting',dict(item_id=detail['id']))
                    result=browser.submit(detail['id'],before_write)
                    after=browser.detail(detail['id'])
                    verified=str(after.get('booleanRegistration'))=='1'
                    store.transition(job_id,'submitting','verified' if verified else 'uncertain',
                        dict(item=summary(after),registration_verified=verified,response_success=result.get('success') is True,credit_hours_awarded=False))
                    break
                # Stay alive across the opening boundary; never submit before opens_at.
                delay=job.get('poll_seconds',10)
                seconds_to_open=(stamp(job['opens_at'])-clock()).total_seconds()
                if seconds_to_open>0:delay=min(delay,seconds_to_open)
                wait_cancellable(store,job_id,delay,sleep)
    except Exception as exc:
        current=store.view(job_id);state=current['state']
        if state in {'monitoring','submitting'}:
            result=current.get('result') or {}
            result.update(reason=str(exc) if isinstance(exc,YoungError) else '后台读取或提交中断；检查状态，禁止重发写请求。')
            if hasattr(exc,'candidates'):result['candidates']=exc.candidates
            store.transition(job_id,state,'uncertain' if state=='submitting' else 'stopped',result)
    return store.view(job_id)


def wait_cancellable(store,job_id,seconds,sleep):
    while seconds>0 and store.view(job_id)['state']=='monitoring':
        step=min(5,seconds);sleep(step);seconds-=step


def run_schedule(schedule_id):
    """Entry called by pythonw from Windows Task Scheduler, not by an AI turn."""
    from .scheduler import read_spec, write_spec
    spec=read_spec(schedule_id)
    while datetime.now(timezone.utc)<stamp(spec['monitor_from']):
        time.sleep(max(0,min(5,(stamp(spec['monitor_from'])-datetime.now(timezone.utc)).total_seconds())))
        spec=read_spec(schedule_id)
        if spec.get('cancelled'):return
    if spec.get('cancelled'):return
    spec.update(worker_started_at=datetime.now(timezone.utc).isoformat(),worker_state='starting')
    write_spec(schedule_id,spec)
    # A pending task may be installed before the full project name is supplied.
    # It never guesses a title or submits until a concrete, authorized job is bound.
    while not spec.get('job_id'):
        if spec.get('worker_state')!='needs_exact_name':
            spec['worker_state']='needs_exact_name';write_spec(schedule_id,spec)
        time.sleep(10);spec=read_spec(schedule_id)
        if spec.get('cancelled'):return
    try:
        ensure_session()
        result=monitor_job(spec['job_id'])
        spec=read_spec(schedule_id)
        spec.update(worker_state=result['state'],finished_at=datetime.now(timezone.utc).isoformat())
    except Exception:
        spec=read_spec(schedule_id);spec.update(worker_state='stopped',reason='脚本执行失败，检查登录或本地任务状态。')
    write_spec(schedule_id,spec)


def ensure_session():
    """Warm the existing account before monitoring; one saved-login recovery only."""
    from .client import YoungClient
    from .reconnect import start,status
    try:
        YoungClient().check()
        return
    except YoungError as exc:
        if '冷却' in str(exc):raise
    launched=start()
    require(launched.get('started') or launched.get('already_running'),'后台认证恢复未能启动；需要检查登录状态。')
    deadline=time.monotonic()+250
    while time.monotonic()<deadline:
        state=status();stage=(state.get('login_progress') or {}).get('stage')
        if stage=='connected':return
        require(stage not in {'error','cancelled','waiting_for_verification','interrupted'},'自动认证需要人工处理，未提交报名。')
        time.sleep(2)
    raise YoungError('后台认证恢复超时，未提交报名。')
