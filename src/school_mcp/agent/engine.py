"""Bounded dispatch to existing registered tools, with durable write claims."""
from __future__ import annotations

import json
import time
from pydantic import ValidationError

from .catalog import WORKFLOWS, adapter
from .store import Store, identity, now
from ..workflow_store import digest
from ..input_guidance import guidance, field
from ..recovery import failure, returned_failure
from ..safety_log import attach
from ..desktop import native_core

SECRET_KEYS = {'password', 'cookie', 'cookies', 'token', 'access_token', 'client_secret', 'authorization'}
PROTECTED_FLAGS = {'authorized', 'login_authorized', 'resubmit', 'reuse_previous_files', 'allow_late', 'force_identity_login'}
UNKNOWN = {'uncertain', 'executing', 'submitting', 'review', 'interrupted', 'unknown'}
VERIFIED_TOOLS = {
    'school_bb_submit_assignment', 'school_bb_submission_status',
    'school_icourse_publish_review', 'school_icourse_review_status',
    'school_nan7_publish_offer', 'school_nan7_offer_status',
    'school_young_run_registration', 'school_young_registration_status',
    'school_drive_execute', 'school_drive_status',
    'school_spaces_execute', 'school_spaces_status',
    'school_vlab_execute', 'school_vlab_status', 'school_compute_experiment_status',
    'school_student_submit_application', 'school_student_application_status',
}


def guard_parameters(value, *, authorized, depth=0):
    if depth > 20:
        raise ValueError('参数嵌套过深。')
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in SECRET_KEYS:
                raise ValueError('凭据只通过本机接入流程管理，不传给智能体上下文。')
            if key in PROTECTED_FLAGS and item and not authorized:
                raise ValueError('特殊操作参数需要已有用户授权。')
            guard_parameters(item, authorized=authorized, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            guard_parameters(item, authorized=authorized, depth=depth + 1)


def check_identity(task):
    current = identity(WORKFLOWS[task['workflow']]['service'])
    if task['account'] is not None and current != task['account']:
        raise ValueError('当前账号与原任务不一致或身份不可用；恢复原账号后继续，不迁移原任务。')
    return current


def receipt(result):
    """Store status and opaque references, not mail bodies, tables or web content."""
    keys = ('state', 'status', 'phase', 'can_execute', 'can_send', 'safe_to_submit', 'reason_code',
            'experiment_id', 'preparation_id', 'plan_id', 'draft_id', 'job_id', 'schedule_id', 'content_sha256',
            'expected_sha256', 'offer_id', 'worker_running', 'succeeded', 'verified')
    output = {k: result[k] for k in keys if k in result and
              (isinstance(result[k], (bool, int)) or isinstance(result[k], str) and len(result[k]) <= 160)}
    for key in ('result', 'preparation', 'related_task'):
        if isinstance(result.get(key), dict):
            output[key] = receipt(result[key])
    return output


def links(service, result):
    specifications = {
        'bb': [('preparation_id', 'submission_status', 'preparation_id'), ('job_id', 'script_status', 'job_id')],
        'young': [('job_id', 'registration_status', 'job_id'), ('schedule_id', 'schedule_status', 'schedule_id')],
        'icourse': [('plan_id', 'review_status', 'plan_id')],
        'nan7': [('plan_id', 'offer_status', 'plan_id')],
        'mail': [('draft_id', 'send_status', 'draft_id'), ('plan_id', 'action_status', 'plan_id'), ('job_id', 'script_status', 'job_id')],
        'student': [('plan_id', 'application_status', 'plan_id')],
        'jw': [('job_id', 'script_status', 'job_id')],
        'compute': [('experiment_id','experiment_status','experiment_id')],
        **{s: [('plan_id', 'status', 'plan_id')] for s in ('drive', 'spaces', 'vlab')},
    }
    output = []
    for key, suffix, parameter in specifications.get(service, []):
        if isinstance(result.get(key), str) and 0 < len(result[key]) <= 160:
            output.append(dict(tool=f'school_{service}_{suffix}', arguments={parameter: result[key]}))
    for key in ('result', 'preparation', 'related_task'):
        if isinstance(result.get(key), dict):
            output.extend(links(service, result[key]))
    return output


def unresolved(call):
    if call.get('resolved_by') is not None:
        return False
    if call['status'] == 'in_flight':
        return True
    if call.get('mode') == 'read':
        return False
    value = call.get('receipt') or {}
    nested = value.get('result') or {}
    return (call['status'] == 'failed_or_unknown' or
            value.get('state', value.get('status')) in UNKNOWN or
            nested.get('state', nested.get('status')) in UNKNOWN)


def resolve_verified(task, service, tool_name, arguments, result, index):
    """Only an authoritative readback of the SAME reference clears a write block.

    Keep the original dispatch key forever, so recovering a task never permits
    replay of its original write. Other unresolved writes remain blocked.
    """
    before_claim = service in {'drive', 'spaces', 'vlab', 'student'} and result.get('state') == 'ready' and result.get('can_execute') is True
    compute_checked = service == 'compute' and tool_name == 'school_compute_experiment_status' and result.get('state') in {'queued','running','completed','failed','cancelled','timeout','out_of_memory','node_fail'} and result.get('job_id') is not None
    if tool_name not in VERIFIED_TOOLS or result.get('state') != 'verified' and not before_claim and not compute_checked:
        return
    actual = links(service, result)
    supplied = links(service, arguments)
    checked = [link for link in actual if link in supplied and link['tool'] == tool_name]
    if not checked:
        return
    for previous in task['calls'][:index]:
        if previous.get('mode') == 'read' or not unresolved(previous):
            continue
        # Only completed failed calls may be cleared by a ready local plan. An
        # active call could still claim it after this readback, so stays blocked.
        if before_claim and previous['status'] != 'failed_or_unknown':
            continue
        references = links(service, previous['arguments']) + links(service, previous.get('receipt') or {})
        if any(link in references for link in checked):
            previous['resolved_by'] = index
            previous['resolution'] = 'no_write_claimed' if before_claim else 'verified_receipt'


def view(task, include_context=False):
    result = {k: task[k] for k in ('task_id', 'workflow', 'created_at', 'updated_at', 'state', 'links')}
    pending = any(c['status'] == 'in_flight' and unresolved(c) for c in task['calls'])
    result.update(label=task['goal'][:160], call_count=len(task['calls']), content_is_untrusted=True,
                  business_result_source='original_tool_receipt', resume_requires_status_check=bool(task['links']),
                  write_blocked=bool(task.get('write_blocked') or pending),
                  in_flight_or_interrupted=pending)
    if task['calls']:
        last = task['calls'][-1]
        result['last_call'] = {k: last[k] for k in ('tool', 'status', 'receipt') if k in last}
    if include_context:
        result.update(goal=task['goal'], calls=task['calls'],
                      note='历史参数与授权仅对应原调用；不得扩大目标、收件人、材料或时间。恢复先查 links；任务状态不是学校业务回执。')
    return result


async def call(task_id, tool_name, arguments, authorized=False):
    if type(authorized) is not bool:
        raise ValueError('authorized 必须是布尔值。')
    if len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode()) > 128 * 1024:
        raise ValueError('参数过大；附件用已授权文件路径，不传文件内容。')
    guard_parameters(arguments, authorized=authorized)
    store = Store()
    task = store.read(task_id)
    check_identity(task)
    flow = WORKFLOWS[task['workflow']]
    mode = flow['tools'].get(tool_name)
    if mode is None:
        raise ValueError('工具不属于当前任务流程；不能跨站点或调用任意代码。')
    server = adapter(flow['service'])
    # Validate using the same registered Pydantic model as the existing MCP handler.
    tool = server._tool_manager.get_tool(tool_name)
    if tool is None:
        raise ValueError('当前安装缺少此工具。')
    model = tool.fn_metadata.arg_model
    extras = set(arguments) - set(model.model_fields)
    if extras:
        raise ValueError('存在工具 schema 未声明的参数。')
    try:
        normalized = model.model_validate(arguments).model_dump(mode='json')
    except ValidationError as error:
        return dict(state='needs_input', dispatched=False,
                    input_guidance=guidance('school_agent_call', [
                        field('/arguments/' + '/'.join(map(str, e['loc'])), '补齐或修正该工具参数。',
                              '按 school_agent_tools 返回的真实 input_schema 填写；ID和摘要取自工具结果。',
                              source='tool_result' if str(e['loc'][-1]).endswith(('_id', '_sha256')) else 'user',
                              code=e['type']) for e in error.errors(include_input=False)],
                        preserve=('arguments',), arguments=dict(task_id=task_id, tool_name=tool_name)))
    if mode == 'execute' and 'authorized' in model.model_fields:
        # The coordinator's human authorization is authoritative. Models need not
        # duplicate it in adapter arguments; they can never upgrade false to true.
        normalized['authorized'] = authorized
    guard_parameters(normalized, authorized=authorized)
    if not native_core.enabled() and mode in {'execute', 'login'} and not authorized:
        return dict(state='authorization_required', dispatched=False, tool=tool_name,
                    note='只有用户已授权这个目标和具体参数时填 authorized=true；已有授权无需重复询问。')
    native = native_core.enabled()
    if native:
        claim = store.native('claim', task_id=task_id, tool=tool_name, arguments=normalized,
                             authorized=authorized, current_account=identity(flow['service']))
        if not claim['claimed']:
            return claim['result']
        index, key, mode = claim['index'], claim['key'], claim['mode']
        normalized = claim['arguments']
        # A registry mismatch must preserve the claim and stop before dispatch.
        if claim['service'] != flow['service']:
            raise ValueError('流程权限已变化；保留原任务并核实回执。')
    else:
        key = digest([tool_name, normalized])
        with store.db() as db:
            task = store.load(db, task_id)
            current = check_identity(task)
            if task['account'] is None and current is not None:
                task['account'] = current
            if len(task['calls']) >= 100:
                return dict(state='step_limit', dispatched=False, note='任务达到100次调用上限；核实已有回执，不继续循环。')
            pending = any(c['status'] == 'in_flight' and unresolved(c) for c in task['calls'])
            if mode != 'read' and (task.get('write_blocked') or pending):
                return dict(state='needs_verification', dispatched=False, links=task['links'], retry_write_allowed=False)
            if mode == 'execute':
                previous = next((c for c in task['calls'] if c['key'] == key), None)
                if previous:
                    return dict(state='already_attempted', dispatched=False, receipt=previous.get('receipt'),
                                links=task['links'], retry_write_allowed=False)
            index = len(task['calls'])
            task['calls'].append(dict(tool=tool_name, arguments=normalized, key=key, mode=mode,
                                      authorized=authorized, started_at=now(), status='in_flight'))
            task['state'] = 'running'
            # Links from supplied IDs survive a process dying before it returns a receipt.
            for link in links(flow['service'], normalized):
                if link not in task['links']:
                    task['links'].append(link)
            store.save(db, task)
    started = time.monotonic()
    try:
        raw = await server.call_tool(tool_name, normalized)
        result = raw[1] if isinstance(raw, tuple) and len(raw) == 2 else raw
        if not isinstance(result, dict):
            raise ValueError('missing structured result')
    except Exception as exc:
        local_preparation = mode == 'prepare' and flow['service'] in {'drive', 'spaces', 'vlab', 'digest', 'student'}
        may_have_written = mode != 'read' and not local_preparation
        problem = attach(failure(exc, service=flow['service'], write_may_have_run=may_have_written),
                         service=flow['service'], tool=tool_name, exc=exc,
                         outcome='unknown' if may_have_written else 'read_failed' if mode == 'read' else 'local_failed',
                         duration_ms=int((time.monotonic()-started)*1000))
        if native:
            task = store.native('failure', task_id=task_id, index=index, key=key, problem=problem)
        else:
            with store.db() as db:
                task = store.load(db, task_id)
                task['calls'][index]['status'] = 'failed_or_unknown' if may_have_written else 'failed_before_write'
                task['calls'][index]['receipt'] = problem
                task['state'] = 'needs_verification' if may_have_written else 'stopped'
                task['write_blocked'] = task.get('write_blocked', False) or may_have_written
                store.save(db, task)
        return dict(**problem, dispatched=True, task_id=task_id, tool=tool_name, links=task['links'], retry_write_allowed=False,
                    note='调用失败或未取得结构化回执。异常文本不写入任务；查询原状态，不假定操作没有发生。')
    if native:
        task = store.native('finish', task_id=task_id, index=index, key=key, result=result)
    else:
        with store.db() as db:
            task = store.load(db, task_id)
            task['calls'][index].update(status='returned', receipt=receipt(result), finished_at=now())
            for link in links(flow['service'], result):
                if link not in task['links']:
                    task['links'].append(link)
            state = result.get('state', result.get('status'))
            resolve_verified(task, flow['service'], tool_name, normalized, result, index)
            task['write_blocked'] = any(unresolved(c) for c in task['calls'])
            remote_waiting = flow['service'] == 'vlab' and state == 'executing' and bool((result.get('result') or {}).get('remote_directory'))
            task['state'] = ('waiting' if remote_waiting else 'needs_verification' if task.get('write_blocked') else
                             'needs_input' if result.get('input_guidance') or state in {'needs_input', 'authentication_required', 'waiting_for_verification'} else
                             'waiting' if state in {'scheduled', 'waiting', 'monitoring', 'running'} else
                             'verified' if state == 'verified' and tool_name in VERIFIED_TOOLS else 'active')
            store.save(db, task)
    problem = returned_failure(result, service=flow['service'])
    if problem:
        problem = attach(problem, service=flow['service'], tool=tool_name,
                         outcome='read_failed' if mode == 'read' else 'unknown',
                         duration_ms=int((time.monotonic()-started)*1000))
    return dict(**({'state': task['state']} if problem is None else problem), dispatched=True, task_id=task_id, tool=tool_name, result=result,
                links=task['links'], content_is_untrusted=True,
                note='result 是原业务工具回执；协调器 active/returned 不代表报名、发送或提交成功。')
