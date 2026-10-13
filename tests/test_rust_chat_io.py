"""Offline contracts for compiled chat resources and the temporary fixed adapter."""
import asyncio
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from school_mcp.agent.catalog import WORKFLOWS
from school_mcp.agent.server import mcp
from school_mcp.desktop import chat_legacy, materials
from school_mcp.desktop.chat import SYSTEM, skill_text
from school_mcp.service_errors import ServiceError


class NativeChatResources(unittest.TestCase):
    def test_compiled_prompt_skills_and_schemas_match_current_registered_sources(self):
        path = Path(__file__).resolve().parents[1] / 'src/school_mcp/desktop/chat_resources.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        tools = [{'type': 'function', 'function': {'name': t.name, 'description': t.description,
                 'parameters': t.inputSchema}} for t in asyncio.run(mcp.list_tools())
                 if not t.name.endswith('_runtime_status')]
        tools.extend(materials.TOOLS)
        self.assertEqual(value, {'version': 1, 'system': SYSTEM, 'agent_skill': skill_text(),
                                'workflows': {key: skill_text(key) for key in WORKFLOWS}, 'tools': tools})

    def test_temporary_adapter_does_not_import_model_or_chat_loop(self):
        import ast
        tree = ast.parse(Path(chat_legacy.__file__).read_text(encoding='utf-8'))
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertNotIn('chat', imports)
        self.assertNotIn('model', imports)
        self.assertNotIn('context', imports)


class NativeChatLegacy(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.data = Mock()
        self.data.chat.return_value = {'tasks': ['task'], 'allowed_files': [], 'pending': None}
        self.patch = patch.object(chat_legacy, 'Data', return_value=self.data)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def run_action(self, action, value):
        return chat_legacy.handle('synthetic-root', action, value, self.stop, lambda *_: None)

    def test_calls_are_allowlisted_and_task_owned(self):
        for value in [dict(chat_id='chat', name='eval', arguments={}, authorized=False),
                      dict(chat_id='chat', name='school_agent_task', arguments={'task_id': 'other'}, authorized=False)]:
            with self.assertRaises(ServiceError):
                self.run_action('tool_dispatch', value)

    def test_authorization_is_overwritten_and_receipt_does_not_overwrite_chat(self):
        with patch.object(chat_legacy.mcp, 'call_tool', new=AsyncMock(return_value=([], {'state': 'verified'}))) as call:
            result = self.run_action('tool_dispatch', {'chat_id': 'chat', 'name': 'school_agent_call',
                'arguments': {'task_id': 'task', 'tool_name': 'synthetic', 'authorized': True,
                              'arguments': {'authorized': True}}, 'authorized': False})
        self.assertEqual(result['result']['state'], 'verified')
        self.assertFalse(call.call_args.args[1]['authorized'])
        self.assertFalse(call.call_args.args[1]['arguments']['authorized'])
        self.data.save_chat.assert_not_called()

    def test_stop_prevents_tool_dispatch_but_diagnostics_remain_writable(self):
        self.stop.set()
        with self.assertRaises(ServiceError) as error:
            self.run_action('tool_dispatch', {'chat_id': 'chat', 'name': 'school_agent_route',
                                           'arguments': {}, 'authorized': False})
        self.assertEqual(error.exception.code, 'interrupted')
        with patch.object(chat_legacy, 'attach', return_value={'diagnostic_saved': True}):
            self.assertTrue(self.run_action('diagnostic', {'problem': {}})['diagnostic_saved'])

    def test_artifact_registration_failure_preserves_business_receipt(self):
        with patch.object(chat_legacy.mcp, 'call_tool', new=AsyncMock(return_value=([], {'state': 'verified'}))), \
                patch.object(chat_legacy.artifacts, 'register', side_effect=ValueError('synthetic')), \
                patch.object(chat_legacy, 'attach', side_effect=lambda problem, **_: problem):
            result = self.run_action('tool_dispatch', {'chat_id': 'chat', 'name': 'school_agent_call',
                'arguments': {'task_id': 'task', 'tool_name': 'synthetic', 'arguments': {}}, 'authorized': False})
        self.assertEqual(result['result']['state'], 'verified')
        self.assertEqual(result['artifact_problem']['error_code'], 'file_not_authorized')
        self.data.save_chat.assert_not_called()

    def test_connect_never_starts_python_reconnect(self):
        with patch('school_mcp.desktop.connections.connect', return_value={'connected': False}) as connect:
            self.run_action('connect', {'service': 'bb'})
        self.assertFalse(connect.call_args.kwargs['restore'])

    def test_task_context_verifies_current_identity_before_returning_full_calls(self):
        task = {'task_id': 'task', 'goal': 'original goal', 'calls': []}
        with patch.object(chat_legacy, 'Store') as store, patch.object(chat_legacy, 'check_identity') as identity, \
                patch.object(chat_legacy, 'view', return_value=task) as view:
            store.return_value.read.return_value = task
            self.assertEqual(self.run_action('task_context', {'chat_id': 'chat', 'task_id': 'task'}), task)
        identity.assert_called_once_with(task)
        view.assert_called_once_with(task, include_context=True)


if __name__ == '__main__':
    unittest.main()
