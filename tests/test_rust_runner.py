"""Actual native turn decisions; model and site IO use synthetic responses."""
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from school_mcp.desktop import native_core
from school_mcp.desktop.app import Application
from school_mcp.desktop.chat import ChatRunner
from school_mcp.desktop.model import ModelClient
from school_mcp.desktop.storage import write_private

CORE = Path(os.environ.get('SCHOOL_TEST_CORE', Path(__file__).resolve().parents[1] / 'native/core/target/debug/school-core.exe'))


@unittest.skipUnless(os.name == 'nt' and CORE.is_file(), 'Windows native core required')
class RustRunnerTests(unittest.TestCase):
    def setUp(self):
        native_core.shutdown()
        env = patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': str(CORE)})
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(native_core.shutdown)
        self.app = Application(tmp.name)
        self.data = self.app.data
        write_private(self.data.settings_path, {'base_url': 'https://model.example/v1', 'model': 'synthetic',
                                              'api_key': 'synthetic-only', 'tested': True})

    def test_actual_driver_uses_rust_decisions_and_keeps_rust_catalog(self):
        chat = self.data.new_chat()
        response = {'role': 'assistant', 'tool_calls': [{'type': 'function', 'id': 'one',
            'function': {'name': 'school_agent_route', 'arguments': '{"goal":"财务报销材料"}'}}]}
        runner = ChatRunner(self.data, threading.Event())
        with patch.object(ModelClient, 'complete', side_effect=[response, {'role':'assistant','content':'synthetic complete'}]), \
             patch.object(ChatRunner, 'needs_authorization', side_effect=AssertionError('legacy decision')), \
             patch.object(ChatRunner, 'operation_label', side_effect=AssertionError('legacy decision')), \
             patch('school_mcp.desktop.chat.validated_message', side_effect=AssertionError('legacy validation')), \
             patch('school_mcp.desktop.chat.tool_arguments', side_effect=AssertionError('legacy argument parser')), \
             patch.object(native_core, 'call', wraps=native_core.call) as calls:
            result = runner.run(chat['id'], 'synthetic task')
        self.assertEqual(result['state'], 'completed')
        operations = [call.args[0] for call in calls.call_args_list]
        self.assertIn('runner_step', operations)
        self.assertIn('catalog_route', operations)
        self.assertTrue(any(m['role']=='tool' for m in self.data.chat(chat['id'])['messages']))

    def test_unknown_model_tool_stops_before_io(self):
        chat = self.data.new_chat()
        runner = ChatRunner(self.data, threading.Event())
        response = {'role': 'assistant', 'tool_calls': [{'type': 'function', 'id': 'one',
            'function': {'name': 'arbitrary_browser', 'arguments': '{}'}}]}
        with patch.object(ModelClient, 'complete', return_value=response), \
             patch.object(runner, 'dispatch', side_effect=AssertionError('must stop before IO')):
            result = runner.run(chat['id'], 'synthetic task', True)
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(result['error_code'], 'invalid_arguments')
        self.assertIsNone(self.data.chat(chat['id'])['pending'])

    def test_invalid_cooldown_number_keeps_original_failure_contract(self):
        chat = self.data.new_chat()
        runner = ChatRunner(self.data, threading.Event())
        response = {'role': 'assistant', 'tool_calls': [{'type':'function','id':'one',
            'function':{'name':'school_agent_route','arguments':'{}'}}]}
        with patch.object(ModelClient, 'complete', return_value=response) as model, \
             patch.object(runner, 'dispatch', new_callable=AsyncMock,
                          return_value={'state':'cooldown','retry_after_seconds':float('nan')}):
            result = runner.run(chat['id'], 'synthetic task')
        self.assertEqual(result['error_code'], 'cooldown')
        self.assertEqual(model.call_count, 1)
        self.assertFalse(result['recovery']['automatic_retry'])


if __name__ == '__main__':
    unittest.main()
