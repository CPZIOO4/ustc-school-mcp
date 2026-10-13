"""Deterministic connection preparation before business calls; never repeats writes."""
import asyncio
import importlib
import time

from ..agent.catalog import adapter
from ..login_runtime import SERVICES, progress
from ..recovery import failure, returned_failure


# These tools operate locally or inspect setup/progress; they must work offline.
LOCAL_SUFFIXES = {'status', 'auth_status', 'setup_guide', 'bind_status', 'entry_points', 'workflow_guide',
                  'preflight', 'plan_timetables', 'script_status', 'schedule_status', 'tasks',
                  'registration_status', 'submission_status', 'send_status', 'action_status',
                  'get_classification_rules', 'list_drafts', 'offer_status', 'review_status'}


def needs_connection(service, tool, mode):
    if service in {'portal', 'student', 'spaces', 'vlab', 'web', 'drive', 'digest'}:
        return False  # Each operation restores and checks its session in one browser.
    suffix = tool.removeprefix(f'school_{service}_')
    return mode in {'read', 'prepare', 'execute'} and suffix not in LOCAL_SUFFIXES


def connect(service, stopped, notify, *, force=False, restore=True):
    def check():
        try:
            raw = asyncio.run(adapter(service).call_tool(f'school_{service}_check_connection', {}))
            value = raw[1]
            return returned_failure(value, service=service) or value
        except Exception as exc:
            return failure(exc, service=service)

    if stopped.is_set():
        return {'state': 'stopped', 'connected': False}
    if not force or service in {'portal', 'student', 'spaces', 'vlab', 'drive'}:
        value = check()
        if value.get('connected') is True:
            return value
        if not restore or service not in SERVICES or value.get('error_code') not in {'authentication_required', 'identity_required'}:
            return value
    if service not in SERVICES:
        return failure(code='authentication_required', service=service)
    notify('restoring_session', '正在使用学校登录状态连接平台…')
    try:
        started = importlib.import_module(f'school_mcp.{service}.reconnect').start()
        if not (started.get('started') or started.get('already_running')):
            return failure(code='cooldown' if started.get('retry_after_seconds') else
                           'login_busy' if started.get('busy_service') else 'login_failed',
                           retry_after_seconds=started.get('retry_after_seconds'), service=service)
        deadline = time.monotonic() + 280
        while time.monotonic() < deadline:
            state = progress(service)
            info = state.get('login_progress') or {}
            stage = info.get('stage')
            notify(stage or 'authenticating', info.get('detail', '正在建立平台会话。'))
            if stage in {'waiting_for_verification', 'error', 'cancelled', 'interrupted'}:
                return failure(code='manual_verification_required' if stage == 'waiting_for_verification' else 'login_failed', service=service)
            if not state.get('login_running'):
                return check()
            if stopped.wait(1):
                return {'state': 'stopped', 'connected': False}
        return failure(code='timeout', service=service)
    except Exception as exc:
        return failure(exc, service=service)
