"""Fixed temporary adapters for native chat IO; no model or turn coordination.

Only business MCP handlers, material parsing and diagnostics remain here. The
Rust caller owns the transcript, permissions, model request and pending claim.
"""
import asyncio

from .storage import Data
from . import artifacts, materials
from ..agent.server import mcp
from ..agent.store import Store
from ..agent.engine import check_identity, view
from ..service_errors import ServiceError
from ..safety_log import attach
from ..recovery import failure


NAMES = frozenset({
    'school_agent_route', 'school_agent_start', 'school_agent_tools',
    'school_agent_call', 'school_agent_task', 'school_agent_tasks',
}) | materials.NAMES


def handle(root, action, value, stopped, notify):
    """Called only by the private, fixed-action native compatibility worker."""
    if action not in {'task_context', 'tool_dispatch', 'connect', 'diagnostic'} or not isinstance(value, dict):
        raise ServiceError('未知的本机兼容操作。', code='invalid_arguments')
    data = Data(root)
    if action == 'diagnostic':
        return attach(value['problem'], stage=value.get('stage', 'tool'),
                      service=value.get('service', 'app'), tool=value.get('tool'),
                      outcome=value.get('outcome', 'unknown'))
    if stopped.is_set():
        raise ServiceError('操作已停止。', code='interrupted')
    if action == 'connect':
        from .connections import connect
        # Native Bridge owns recovery, including the fixed identity_restore
        # provider. This low-level action only checks the resulting session.
        return connect(value['service'], stopped, notify, restore=False)
    chat = data.chat(value['chat_id'])
    if action == 'task_context':
        if value.get('task_id') not in chat['tasks']:
            raise ServiceError('任务不属于当前对话。', code='task_access_denied')
        task = Store().read(value['task_id'])
        check_identity(task)
        return view(task, include_context=True)
    name, arguments = value.get('name'), value.get('arguments')
    authorized = value.get('authorized')
    if name not in NAMES or not isinstance(arguments, dict) or type(authorized) is not bool:
        raise ServiceError('工具或参数无效。', code='invalid_arguments')
    if name in {'school_agent_tools', 'school_agent_call', 'school_agent_task'} and arguments.get('task_id') not in chat['tasks']:
        raise ServiceError('任务不属于当前对话。', code='task_access_denied')
    artifact_problem = None
    if name in materials.NAMES:
        result = materials.call(data, chat, name, arguments)
    else:
        if name == 'school_agent_call':
            arguments = {**arguments, 'authorized': authorized}
            nested = arguments.get('arguments')
            if isinstance(nested, dict) and 'authorized' in nested:
                arguments['arguments'] = {**nested, 'authorized': authorized}
        # Recheck after private-record/schema work, before the single dispatch.
        if stopped.is_set():
            raise ServiceError('操作已停止。', code='interrupted')
        raw = asyncio.run(mcp.call_tool(name, arguments))
        if not isinstance(raw, tuple) or not isinstance(raw[1], dict):
            raise ServiceError('工具未返回结构化回执。', code='runtime_failure')
        result = raw[1]
        if name == 'school_agent_call':
            try:
                artifacts.register(data, chat, arguments.get('tool_name'), result)
            except Exception as exc:
                # A local file validation failure must not erase a business
                # receipt that has already returned from the actual handler.
                artifact_problem = attach(failure(code='file_not_authorized'), exc=exc,
                                          stage='document', outcome='local_failed')
    # Never save the stale chat snapshot from this adapter. Rust persists the
    # returned receipt first and validates any newly produced files itself.
    return {'result': result, 'artifacts': chat.get('artifacts', []),
            **({'artifact_problem': artifact_problem} if artifact_problem else {})}
