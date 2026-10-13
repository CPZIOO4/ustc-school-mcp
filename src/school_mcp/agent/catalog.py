"""Fixed capability registry, with Rust ownership in the native application.

The JSON is a public, reviewed export of local registrations, never website or
model output. Native lookups use the compiled registry through the private pipe.
Python remains the adapter/schema runtime for school services during migration.
"""
from collections.abc import Mapping
from copy import deepcopy
from importlib import import_module
import json
from pathlib import Path

from ..desktop import native_core


_CATALOG = json.loads(Path(__file__).with_name('catalog_data.json').read_text(encoding='utf-8'))
ENTRY_TOOLS = _CATALOG['entry_tools']


def workflows():
    if native_core.enabled():
        return native_core.call('catalog_workflows', {})
    return deepcopy(_CATALOG['workflows'])


def get_workflow(key):
    if native_core.enabled():
        return native_core.call('catalog_workflow', {'workflow': key})
    if key not in _CATALOG['workflows']:
        raise ValueError('未知学校业务流程。')
    return deepcopy(_CATALOG['workflows'][key])


def mode(key, tool_name):
    if native_core.enabled():
        return native_core.call('catalog_mode', {'workflow': key, 'tool_name': tool_name})
    value = get_workflow(key)['tools'].get(tool_name)
    if value is None:
        raise ValueError('工具不属于当前任务流程；不能跨站点或调用任意代码。')
    return value


class _Workflows(Mapping):
    """Keep existing callers without loading the core during module imports.

    Returned flows are copies; modifying one never extends the trusted registry.
    Loading lazily also avoids a model/storage/catalog import cycle at startup.
    """
    def __getitem__(self, key):
        return get_workflow(key)

    def __iter__(self):
        return iter(workflows())

    def __len__(self):
        return len(workflows())

    def __contains__(self, key):
        return key in workflows()

    def get(self, key, default=None):
        return workflows().get(key, default)

    def items(self):
        return workflows().items()

    def values(self):
        return workflows().values()


WORKFLOWS = _Workflows()


def adapter(service):
    name = 'school_mcp.server' if service == 'mail' else f'school_mcp.{service}.server'
    return import_module(name).mcp


def entry_schema(key):
    """Return one registered read-tool schema; no network or tool dispatch."""
    if native_core.enabled():
        return native_core.call('catalog_entry', {'workflow': key})
    flow = get_workflow(key)
    name = f'school_{flow["service"]}_{ENTRY_TOOLS[key]}'
    tool = adapter(flow['service'])._tool_manager.get_tool(name)
    if tool is None or flow['tools'].get(name) != 'read':
        raise ValueError('默认查询工具未注册。')
    return dict(name=name, mode='read', description=tool.description,
                input_schema=tool.fn_metadata.arg_model.model_json_schema())


def route(goal):
    if native_core.enabled():
        return native_core.call('catalog_route', {'goal': goal})
    text = goal.casefold()
    candidates = [dict(workflow=key, label=w['label']) for key, w in WORKFLOWS.items()
                  if any(word in text for word in w['keywords'])]
    return dict(state='choose_workflow' if candidates else 'needs_input', candidates=candidates,
                routing_method='keyword_hints_only', automatic_execution=False,
                note='由宿主 AI 理解完整用户意图并选择流程；多个事项分别建任务。关键词不代表完整意图或操作授权。')


async def describe(key, tool_name=None):
    if native_core.enabled():
        return native_core.call('catalog_describe', {'workflow': key, 'tool_name': tool_name})
    flow = get_workflow(key)
    registered = {t.name: t for t in await adapter(flow['service']).list_tools()}
    names = [tool_name] if tool_name else flow['tools']
    result = []
    for name in names:
        if name not in flow['tools'] or name not in registered:
            raise ValueError('工具不属于此流程或当前版本未提供。')
        tool = registered[name]
        item = dict(name=name, mode=flow['tools'][name], description=tool.description)
        if tool_name:
            item['input_schema'] = tool.inputSchema
        result.append(item)
    return dict(workflow=key, label=flow['label'], tools=result,
                skill_reference=f'.agents/skills/school-services/references/{flow["reference"]}',
                instructions=adapter(flow['service']).instructions,
                schema_included=bool(tool_name), limitations=deepcopy(_CATALOG['limitations']))
