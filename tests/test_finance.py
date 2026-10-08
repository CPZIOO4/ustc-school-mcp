import json
import unittest
from unittest.mock import patch
import httpx
from network_test_support import isolate_pacing as setUpModule
from school_mcp.finance.client import FinanceClient, portal_entries, smart_entry, request_allowed
from school_mcp.finance.session import FinanceError, own_state, load_session

PORTAL = '<html><div id="YBX" url="../YBX/main2.jsp?context=synthetic-private-context&amp;pId=YBX"></div></html>'


class FinanceTests(unittest.TestCase):
    def test_document_workflow_does_not_claim_live_permission(self):
        from school_mcp.finance.workflows import workflow_guide
        self.assertEqual(len(workflow_guide()['business_types']), 5)
        for kind in ['daily', 'travel', 'loan', 'remuneration', 'internal_transfer']:
            result = workflow_guide(kind)
            self.assertFalse(result['writes_supported'])
            self.assertFalse(result['live_form_verified'])
            self.assertFalse(result['network_checked'])
            self.assertTrue(result['specific_information'])
        with self.assertRaises(FinanceError): workflow_guide('arbitrary')

    def test_portal_entries_never_return_sso_context(self):
        entries = portal_entries(PORTAL)
        self.assertEqual(entries[0]['service_id'], 'YBX')
        self.assertNotIn('synthetic-private-context', json.dumps(entries))
        self.assertFalse(entries[0]['business_operations_verified'])

    def test_smart_entry_resolves_only_observed_target(self):
        self.assertTrue(smart_entry(PORTAL).startswith('https://cwzh.ustc.edu.cn/YBX/main2.jsp?'))
        for value in ['https://evil.example/YBX/main2.jsp', 'http://cwzh.ustc.edu.cn/YBX/main2.jsp',
                      'https://cwzh.ustc.edu.cn:444/YBX/main2.jsp', '../YBX/save.action']:
            with self.assertRaises(FinanceError): smart_entry(f'<div id="YBX" url="{value}"></div>')

    def test_guard_only_reviewed_initialization_and_static(self):
        base = 'https://cwzh.ustc.edu.cn'
        self.assertTrue(request_allowed(base + '/YBX/main.jsp', 'GET'))
        self.assertTrue(request_allowed(base + '/YBX/loadRolesMenu.action', 'POST', 'encrypted-envelope'))
        self.assertTrue(request_allowed(base + '/YBX/redirectCustomPage.action?url=Customize%2FCustomJsList.json', 'POST'))
        for path, method in [('/YBX/save.action', 'GET'), ('/YBX/submit.action', 'POST'),
                             ('/YBX/common_updateUserContext.action', 'POST'), ('/WFManager/logout.jsp', 'GET'),
                             ('/YBX/redirectCustomPage.action?url=../secret', 'POST')]:
            self.assertFalse(request_allowed(base + path, method))
        self.assertFalse(request_allowed('http://cwzh.ustc.edu.cn/YBX/main.jsp', 'GET'))
        self.assertFalse(request_allowed('https://evil.example/a.js', 'GET'))
        self.assertFalse(request_allowed('https://cwzh.ustc.edu.cn@evil.example/a.js', 'GET'))

    def test_identity_is_only_allowed_during_login(self):
        url = 'https://id.ustc.edu.cn/cas/login'
        self.assertFalse(request_allowed(url, 'POST'))
        self.assertTrue(request_allowed(url, 'POST', login=True))

    def test_own_state_excludes_identity_and_other_accounts_sites(self):
        result = own_state({'cookies': [{'domain': 'cwzh.ustc.edu.cn'}, {'domain': '.ustc.edu.cn'}, {'domain': 'evil.example'}],
                            'origins': [{'origin': 'https://cwzh.ustc.edu.cn'}, {'origin': 'https://id.ustc.edu.cn'}]})
        self.assertEqual(len(result['cookies']), 1)
        self.assertEqual(len(result['origins']), 1)

    def test_session_bound_to_current_identity(self):
        with patch('school_mcp.finance.session._load', return_value={'account': 'old', 'state': {}}), \
             patch('school_mcp.finance.session.load_credentials', return_value={'username': 'new'}):
            with self.assertRaises(FinanceError): load_session()

    def test_authenticated_portal_check_and_redirect_refusal(self):
        session = {'state': {'cookies': []}}
        client = FinanceClient(session, httpx.MockTransport(lambda request: httpx.Response(200, text=PORTAL)))
        check = client.check()
        self.assertTrue(check['connected'])
        self.assertEqual(check['scope'], 'financial_portal')
        self.assertFalse(check['business_operations_verified'])
        seen = []
        def redirect(request):
            seen.append(str(request.url))
            return httpx.Response(302, headers={'location': 'https://id.ustc.edu.cn/cas/login'})
        with self.assertRaises(FinanceError): FinanceClient(session, httpx.MockTransport(redirect)).check()
        self.assertEqual(len(seen), 1)

    def test_login_html_not_reported_as_connected(self):
        with self.assertRaises(FinanceError):
            FinanceClient({'state': {'cookies': []}}, httpx.MockTransport(lambda r: httpx.Response(200, text='<form>login</form>'))).check()
