import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from school_mcp.finance.catalog import CatalogBootstrap, catalog_result
from school_mcp.finance.client import request_allowed, install_guard

BASE = 'https://cwzh.ustc.edu.cn/YBX/'


class CatalogTests(unittest.TestCase):
    def bootstrap(self):
        state = CatalogBootstrap()
        state.observe('/YBX/getDefaultWinno.action', 200, 'WF_YBX:6:SY-个人首页')
        self.assertTrue(state.allow(BASE+'common_updateUserContext.action', 'POST', 'synthetic-envelope'))
        state.observe('/YBX/common_updateUserContext.action', 200, 'ok')
        self.assertTrue(state.allow(BASE+'loadDefinition.action', 'POST', 'synthetic-envelope'))
        state.observe('/YBX/loadDefinition.action', 200, '')
        return state

    def test_generic_guard_never_exposes_bootstrap_or_sql_endpoints(self):
        for name in ['common_updateUserContext', 'loadDefinition', 'common_bindSQLDataBackend',
                     'commonUpdate_responseButtonEvent', 'logRemark']:
            self.assertFalse(request_allowed(BASE+name+'.action', 'POST', 'envelope'))

    def test_one_observed_home_context_one_definition_and_one_explicit_read(self):
        state = CatalogBootstrap()
        self.assertFalse(state.allow(BASE+'common_updateUserContext.action', 'POST', 'envelope'))
        state = self.bootstrap()
        self.assertFalse(state.allow(BASE+'common_updateUserContext.action', 'POST', 'envelope'))
        self.assertFalse(state.allow(BASE+'loadDefinition.action', 'POST', 'envelope'))
        self.assertFalse(state.allow(BASE+'common_bindSQLDataBackend.action', 'POST', 'envelope'))
        state.query_armed = True
        self.assertTrue(state.allow(BASE+'common_bindSQLDataBackend.action', 'POST', 'envelope'))
        self.assertFalse(state.allow(BASE+'common_bindSQLDataBackend.action', 'POST', 'envelope'))

    def test_changed_default_or_failed_context_never_advances(self):
        for value in ['WF_YBX:6:填写报销单', 'OTHER:6:SY-个人首页', 'WF_YBX:6:SY-个人首页?ticket=private']:
            state = CatalogBootstrap()
            state.observe('/YBX/getDefaultWinno.action', 200, value)
            self.assertFalse(state.allow(BASE+'common_updateUserContext.action', 'POST', 'envelope'))
        state = CatalogBootstrap()
        state.observe('/YBX/getDefaultWinno.action', 200, 'WF_YBX:6:SY-个人首页')
        self.assertTrue(state.allow(BASE+'common_updateUserContext.action', 'POST', 'envelope'))
        state.observe('/YBX/common_updateUserContext.action', 403, 'ok')
        self.assertFalse(state.allow(BASE+'loadDefinition.action', 'POST', 'envelope'))

    def test_bootstrap_stays_on_fixed_origin_and_never_executes_procedures(self):
        state = self.bootstrap()
        state.query_armed = True
        for url in [BASE+'commonUpdate_responseButtonEvent.action', BASE+'save.action',
                    BASE+'common_bindSQLData.action', BASE+'common_bindSQLDataBackend.action?sql=private',
                    BASE.replace('https:', 'http:')+'common_bindSQLDataBackend.action',
                    BASE.replace('cwzh.ustc.edu.cn', 'evil.example')+'common_bindSQLDataBackend.action',
                    BASE.replace('cwzh.ustc.edu.cn', 'cwzh.ustc.edu.cn:444')+'common_bindSQLDataBackend.action']:
            self.assertFalse(state.allow(url, 'POST', 'envelope'))
        for method, body in [('GET','envelope'), ('POST',''), ('POST','x'*(1024*1024+1))]:
            self.assertFalse(state.allow(BASE+'common_bindSQLDataBackend.action', method, body))

    def test_dependent_request_sees_state_before_response_is_delivered(self):
        state, context = CatalogBootstrap(), Mock()
        install_guard(context, catalog=state)
        guard = context.route.call_args.args[1]
        def route(name, body, reply):
            r = Mock(request=SimpleNamespace(url=BASE+name+'.action', method='POST', post_data=body))
            r.fetch.return_value = Mock(status=200, headers={}, text=Mock(return_value=reply))
            return r
        default = route('getDefaultWinno', '', 'WF_YBX:6:SY-个人首页')
        sync = route('common_updateUserContext', 'envelope', 'ok')
        definition = route('loadDefinition', 'envelope', '{}')
        default.fulfill.side_effect = lambda **_: guard(sync)
        sync.fulfill.side_effect = lambda **_: guard(definition)
        with patch('school_mcp.finance.client.network.limiter'):
            guard(default)
        self.assertTrue(state.definition_ready)
        for r in [default, sync, definition]:
            r.abort.assert_not_called()
            r.fetch.assert_called_once_with(max_redirects=0, timeout=15000)
            r.fulfill.assert_called_once()

    def test_metadata_url_cannot_smuggle_another_operation(self):
        state = CatalogBootstrap()
        state.default_window = 6
        state.context_ready = True
        for suffix in ['?operation=save', '?junk', '?type=W&type=E', '?type=']:
            self.assertFalse(state.allow(BASE+'loadDefinition.action'+suffix, 'POST', 'envelope'))

    def test_catalog_only_returns_labels_not_personal_fields_or_actions(self):
        data = {'total':1, 'entries':[{'code':'synthetic_daily','title':'<b>日常报销</b>', 'group':'申请报销',
                 'amount':'private-amount', 'func':'private-procedure', 'url':'private-ticket'}]}
        result = catalog_result(data)
        self.assertTrue(result['catalog_complete'])
        self.assertEqual(result['business_entries'][0]['guide_business_type'], 'daily')
        self.assertFalse(result['business_entries'][0]['business_operations_verified'])
        self.assertNotIn('private-', json.dumps(result))

    def test_partial_duplicate_changed_shape_and_empty_results_are_distinct(self):
        entry = {'code':'synthetic','title':'日常报销','group':'申请报销'}
        for data in [{'total':2,'entries':[entry]}, {'total':1,'entries':[entry],'truncated':True},
                     {'total':2,'entries':[entry,entry]}, {'total':1,'entries':[{'code':'bad?ticket=secret'}]}]:
            self.assertFalse(catalog_result(data)['catalog_complete'])
        empty = catalog_result({'total':0,'entries':[]})
        self.assertTrue(empty['catalog_complete'])
        self.assertEqual(empty['business_entry_count'],0)
        error = catalog_result({'error':'secret-stack-trace'})
        self.assertEqual(error['catalog_state'],'catalog_shape_changed')
        self.assertNotIn('secret',json.dumps(error))


if __name__ == '__main__':
    unittest.main()
