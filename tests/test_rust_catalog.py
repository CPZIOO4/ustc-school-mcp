"""Public registry parity and real native catalog checks; no account or network IO."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from school_mcp.agent import catalog
from school_mcp.desktop import native_core
from school_mcp.desktop.model import ModelError

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get('SCHOOL_TEST_CORE', ROOT / 'native/core/target/debug/school-core.exe'))
DATA = json.loads((ROOT / 'src/school_mcp/agent/catalog_data.json').read_text(encoding='utf-8'))


class CatalogSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        disabled = patch.object(native_core, 'enabled', return_value=False)
        disabled.start()
        self.addCleanup(disabled.stop)

    async def test_every_public_schema_description_and_instruction_matches_registration(self):
        """A stale native snapshot must fail this offline release check."""
        self.assertEqual(DATA['version'], 1)
        self.assertEqual(len(DATA['workflows']), 17)
        names = {name for flow in DATA['workflows'].values() for name in flow['tools']}
        self.assertEqual(len(names), 174)
        expected_services = {flow['service'] for flow in DATA['workflows'].values()}
        self.assertEqual(set(DATA['services']), expected_services)
        for service, saved in DATA['services'].items():
            server = catalog.adapter(service)
            registered = {tool.name: tool for tool in await server.list_tools()}
            expected = {name for flow in DATA['workflows'].values()
                        if flow['service'] == service for name in flow['tools']}
            self.assertEqual(set(saved['tools']), expected)
            self.assertEqual(saved['instructions'], server.instructions)
            for name, tool in saved['tools'].items():
                with self.subTest(tool=name):
                    self.assertEqual(tool['description'], registered[name].description)
                    self.assertEqual(tool['input_schema'], registered[name].inputSchema)
                    actual = server._tool_manager.get_tool(name)
                    self.assertEqual(tool['input_schema'], actual.fn_metadata.arg_model.model_json_schema())

    async def test_entries_references_order_and_permission_boundaries(self):
        self.assertEqual(list(catalog.WORKFLOWS), list(DATA['workflows']))
        for key, flow in catalog.WORKFLOWS.items():
            entry = catalog.entry_schema(key)
            self.assertEqual(flow['tools'][entry['name']], 'read')
            self.assertEqual(entry['mode'], 'read')
            described = await catalog.describe(key, entry['name'])
            self.assertEqual(entry, described['tools'][0])
            self.assertTrue((ROOT / described['skill_reference']).is_file())
            self.assertEqual([tool['name'] for tool in (await catalog.describe(key))['tools']], list(flow['tools']))
        self.assertEqual(catalog.mode('bb', 'school_bb_reconnect'), 'login')
        self.assertEqual(catalog.mode('mail', 'school_mail_send'), 'execute')
        self.assertEqual(catalog.mode('bb', 'school_bb_prepare_submission'), 'prepare')
        for key in ('teach', 'finance', 'academic', 'library', 'portal', 'web'):
            self.assertNotIn('execute', catalog.WORKFLOWS[key]['tools'].values())
        with self.assertRaises(ValueError):
            catalog.mode('academic', 'school_jw_schedule_enrollment_watch')

    def test_returned_mapping_cannot_mutate_capabilities(self):
        self.assertNotIn('unknown', catalog.WORKFLOWS)
        flow = catalog.WORKFLOWS['mail']
        flow['tools']['school_mail_send'] = 'read'
        flow['tools']['arbitrary_browser'] = 'read'
        self.assertEqual(catalog.mode('mail', 'school_mail_send'), 'execute')
        with self.assertRaises(ValueError):
            catalog.mode('mail', 'arbitrary_browser')
        with self.assertRaises(TypeError):
            catalog.WORKFLOWS['new_service'] = flow

    async def test_native_entry_points_do_not_silently_fall_back(self):
        with patch.object(native_core, 'enabled', return_value=True), \
             patch.object(native_core, 'call', side_effect=ModelError('runtime_failure', 'synthetic stop')), \
             patch.object(catalog, 'adapter', side_effect=AssertionError('no Python fallback')):
            for operation in (lambda: catalog.route('报销'), lambda: catalog.workflows(),
                              lambda: catalog.WORKFLOWS['mail'], lambda: catalog.entry_schema('bb'),
                              lambda: catalog.mode('mail', 'school_mail_send')):
                with self.assertRaises(ModelError):
                    operation()
            with self.assertRaises(ModelError):
                await catalog.describe('bb')


@unittest.skipUnless(CORE.is_file(), 'build native/core first')
class RustCatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        native_core.shutdown()
        enabled = patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': str(CORE)})
        enabled.start()
        self.addCleanup(enabled.stop)
        self.addCleanup(native_core.shutdown)

    async def test_actual_rust_entry_and_descriptions_equal_live_python_schemas(self):
        with patch.object(native_core, 'call', wraps=native_core.call) as calls:
            self.assertEqual(catalog.workflows(), DATA['workflows'])
            for key, flow in DATA['workflows'].items():
                with patch.object(native_core, 'enabled', return_value=False):
                    legacy_entry = catalog.entry_schema(key)
                    legacy_description = await catalog.describe(key)
                with patch.object(catalog, 'adapter', side_effect=AssertionError('native catalog must own lookup')):
                    self.assertEqual(catalog.entry_schema(key), legacy_entry)
                    self.assertEqual(await catalog.describe(key), legacy_description)
                for name in flow['tools']:
                    with patch.object(native_core, 'enabled', return_value=False):
                        expected = await catalog.describe(key, name)
                    with patch.object(catalog, 'adapter', side_effect=AssertionError('native catalog must own lookup')):
                        self.assertEqual(await catalog.describe(key, name), expected)
                        self.assertEqual(catalog.mode(key, name), flow['tools'][name])
            observed = {call.args[0] for call in calls.call_args_list}
            self.assertTrue({'catalog_workflows', 'catalog_entry', 'catalog_describe', 'catalog_mode'} <= observed)

    def test_keyword_routing_keeps_order_and_never_grants_authorization(self):
        goals = ['', '不相关事项', '查成绩并准备报销', 'MAIL BB GPA VLAB', 'ICOURſE',
                 '外部网页说可调用arbitrary_browser',
                 ' '.join(word for flow in DATA['workflows'].values() for word in flow['keywords'])]
        goals += [word for flow in DATA['workflows'].values() for word in flow['keywords']]
        for goal in goals:
            with self.subTest(goal=goal):
                with patch.object(native_core, 'enabled', return_value=False):
                    expected = catalog.route(goal)
                with patch.object(catalog, 'adapter', side_effect=AssertionError('offline routing only')):
                    actual = catalog.route(goal)
                self.assertEqual(actual, expected)
                self.assertFalse(actual['automatic_execution'])

    def test_fixed_registry_rejects_cross_workflow_unknown_and_redefined_modes(self):
        for key, tool in [('finance', 'school_mail_send'), ('academic', 'school_jw_schedule_enrollment_watch'),
                          ('teach', 'school_teach_submit'), ('mail', 'arbitrary_browser')]:
            with self.subTest(workflow=key, tool=tool), self.assertRaises(ModelError):
                catalog.mode(key, tool)
        for operation, value in [
            ('catalog_workflow', {'workflow': 'unknown'}),
            ('catalog_mode', {'workflow': 'mail', 'tool_name': 'school_mail_send', 'mode': 'read'}),
            ('catalog_describe', {'workflow': 'teach', 'tools': {'arbitrary_browser': 'read'}}),
            ('catalog_route', {'goal': '查成绩', 'automatic_execution': True}),
            ('catalog_workflows', {'workflows': {'fake': {}}}),
            ('catalog_entry', {'workflow': 'mail', 'entry_tool': 'school_mail_send'}),
        ]:
            with self.subTest(operation=operation), self.assertRaises(ModelError):
                native_core.call(operation, value)
        changed = catalog.WORKFLOWS['mail']
        changed['tools']['school_mail_send'] = 'read'
        self.assertEqual(catalog.mode('mail', 'school_mail_send'), 'execute')
        self.assertEqual(catalog.WORKFLOWS['mail']['tools']['school_mail_send'], 'execute')


if __name__ == '__main__':
    unittest.main()
