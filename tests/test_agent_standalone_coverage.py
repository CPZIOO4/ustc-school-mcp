"""Keep the coordinator usable when standalone MCP registrations are disabled."""
import os
import tempfile
import unittest
from unittest.mock import patch

from school_mcp.agent import engine
from school_mcp.agent.catalog import WORKFLOWS, adapter, describe
from school_mcp.agent.store import Store


class CoverageTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_nine_service_tools_and_schemas_are_reachable(self):
        for service in ('mail', 'bb', 'jw', 'library', 'teach', 'nan7', 'icourse', 'young', 'finance'):
            registered = {tool.name: tool for tool in await adapter(service).list_tools()}
            flows = {key: flow for key, flow in WORKFLOWS.items() if flow['service'] == service}
            covered = set().union(*(set(flow['tools']) for flow in flows.values()))
            self.assertEqual(set(registered), covered, service)
            for key, flow in flows.items():
                for name in flow['tools']:
                    actual = await describe(key, name)
                    self.assertEqual(actual['tools'][0]['input_schema'], registered[name].inputSchema)

    @unittest.skipUnless(os.name == 'nt', 'Task encryption uses Windows DPAPI')
    async def test_query_login_flag_requires_authorization_before_dispatch(self):
        with tempfile.TemporaryDirectory() as private, patch.dict(os.environ, {'SCHOOL_MCP_LOCAL_DIR': private}):
            task = Store().create('bb', 'synthetic login guard', None)
            with patch.object(engine, 'adapter') as get_adapter:
                with self.assertRaises(ValueError):
                    await engine.call(task['task_id'], 'school_bb_query',
                                      {'operation': 'check', 'login_authorized': True}, False)
                get_adapter.assert_not_called()
            self.assertEqual(Store().read(task['task_id'])['calls'], [])

    @unittest.skipUnless(os.name == 'nt', 'Task encryption uses Windows DPAPI')
    async def test_runtime_check_dispatches_without_network_or_credentials(self):
        with tempfile.TemporaryDirectory() as private, patch.dict(os.environ, {'SCHOOL_MCP_LOCAL_DIR': private}):
            task = Store().create('teach', 'synthetic offline runtime check', 'public')
            result = await engine.call(task['task_id'], 'school_teach_runtime_status', {})
            self.assertTrue(result['dispatched'])
            self.assertEqual(result['result']['service'], 'teach')
            self.assertFalse(result['result']['network_checked'])
            self.assertFalse(result['result']['credentials_read'])
