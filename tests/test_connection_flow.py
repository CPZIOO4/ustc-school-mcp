import unittest
from unittest.mock import patch, MagicMock

import httpx
from network_test_support import isolate_pacing as setUpModule
from school_mcp.connection_flow import ReadSession, OPERATIONS
from school_mcp.service_errors import ServiceError, identity_redirect, policy_error
from school_mcp.bb.session import BBError
from school_mcp.jw.client import JWClient
from school_mcp.bb.client import BBClient
from school_mcp.library.client import LibraryClient
from school_mcp.finance.client import FinanceClient


def expired():
    return ServiceError('private URL must not leak', code='authentication_required')


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.call = self.enterContext(patch.object(ReadSession, '_call', return_value={'courses': []}))
        self.credentials = self.enterContext(patch('school_mcp.bb.identity.load_credentials', return_value={}))
        self.start = self.enterContext(patch('school_mcp.bb.reconnect.start', return_value={'started': True}))
        self.progress = self.enterContext(patch('school_mcp.connection_flow.progress', return_value={
            'login_progress': {'stage': 'connected'}, 'login_running': False}))

    def test_success_does_not_login(self):
        self.assertTrue(ReadSession(True).read('bb', 'courses')['completed'])
        self.start.assert_not_called()

    def test_expired_requires_existing_authorization(self):
        self.call.side_effect = expired()
        self.assertEqual(ReadSession().read('bb', 'courses')['state'], 'authentication_required')
        self.start.assert_not_called()

    def test_recovery_replays_original_read_once(self):
        self.call.side_effect = [expired(), {'courses': []}]
        result = ReadSession(True).read('bb', 'courses', {'term': '2026FA'})
        self.assertTrue(result['completed'])
        self.assertTrue(result['login_recovery_attempted'])
        self.assertEqual(self.call.call_args_list[0], self.call.call_args_list[1])
        self.start.assert_called_once()

    def test_stale_success_still_requires_real_read(self):
        self.call.side_effect = expired()
        self.assertEqual(ReadSession(True).read('bb', 'courses')['state'], 'authentication_failed')
        self.assertEqual(self.call.call_count, 2)
        self.start.assert_called_once()

    def test_one_recovery_per_service_in_workflow(self):
        self.call.side_effect = [expired(), {}, expired()]
        session = ReadSession(True)
        self.assertTrue(session.read('bb', 'courses')['completed'])
        self.assertEqual(session.read('bb', 'announcements')['state'], 'authentication_failed')
        self.start.assert_called_once()

    def test_non_auth_failures_never_login_or_leak(self):
        for error, state in [(ServiceError('private', code='access_denied'), 'access_denied'),
                             (ServiceError('private', code='cooldown', retry_after_seconds=60), 'cooldown'),
                             (ValueError('private malformed JSON'), 'unavailable'),
                             (TimeoutError('private URL'), 'unavailable')]:
            with self.subTest(state=state):
                self.call.side_effect = error
                result = ReadSession(True).read('bb', 'courses')
                self.assertEqual(result['state'], state)
                self.assertNotIn('private', str(result))
        self.start.assert_not_called()

    def test_missing_identity(self):
        self.call.side_effect = expired()
        self.credentials.side_effect = BBError('missing')
        self.assertEqual(ReadSession(True).read('bb', 'courses')['state'], 'identity_required')
        self.start.assert_not_called()

    def test_manual_verification_and_dead_worker_stop(self):
        self.call.side_effect = expired()
        for stage, state in [('waiting_for_verification', 'manual_verification_required'), ('interrupted', 'login_failed')]:
            self.progress.return_value = {'login_progress': {'stage': stage}}
            self.assertEqual(ReadSession(True).read('bb', 'courses')['state'], state)

    def test_bounded_wait_reuses_running_login(self):
        self.call.side_effect = expired()
        self.start.return_value = {'already_running': True, 'busy_service': 'bb'}
        self.progress.return_value = {'login_progress': {'stage': 'starting'}, 'login_running': True}
        self.assertEqual(ReadSession(True, 0).read('bb', 'courses')['state'], 'login_running')
        self.assertEqual(self.call.call_count, 1)

    def test_other_login_and_cooldown(self):
        self.call.side_effect = expired()
        self.start.return_value = {'busy_service': 'jw', 'status_tool': 'school_jw_status'}
        self.assertEqual(ReadSession(True).read('bb', 'courses')['state'], 'login_busy')
        self.start.return_value = {'retry_after_seconds': 60}
        self.assertEqual(ReadSession(True).read('bb', 'courses')['state'], 'cooldown')
        self.progress.assert_not_called()

    def test_mutations_and_bad_parameters_are_not_called(self):
        for operation, params in [('submit', {}), ('courses', []), ('courses', 'x')]:
            self.assertEqual(ReadSession(True).read('bb', operation, params)['state'], 'invalid_arguments')
        self.call.assert_not_called()
        self.start.assert_not_called()

    def test_all_adapters_exclude_mutations(self):
        for methods in OPERATIONS.values():
            self.assertFalse({'send', 'submit', 'publish', 'renew', 'drop'} & set(methods))


class ClassificationTests(unittest.TestCase):
    def clients(self, response):
        transport = httpx.MockTransport(lambda request: response())
        return [(JWClient(cookies=[], transport=transport), '/home'),
                (BBClient(cookies=[], transport=transport), '/webapps/portal/execute/tabs/tabAction'),
                (LibraryClient(cookies=[], base_url='http://opac.lib.ustc.edu.cn', transport=transport), '/reader/redr_info.php')]

    def test_http_auth_vs_denial_vs_external_redirect(self):
        for response, state in [
            (lambda: httpx.Response(401), 'authentication_required'),
            (lambda: httpx.Response(403), 'access_denied'),
            (lambda: httpx.Response(302, headers={'location': 'https://id.ustc.edu.cn/cas/login'}), 'authentication_required'),
            (lambda: httpx.Response(302, headers={'location': 'https://other.example/login'}), 'unavailable'),
            (lambda: httpx.Response(200, headers={'content-type': 'text/html'}, text='<input type="password">'), 'authentication_required')]:
            for client, path in self.clients(response):
                with self.subTest(client=type(client).__name__, state=state):
                    with self.assertRaises(ServiceError) as caught:
                        client.get(path)
                    self.assertEqual(caught.exception.code, state)

    def test_bad_signature_does_not_load_account(self):
        with patch('school_mcp.bb.client.load_session') as load:
            result = ReadSession(True).read('bb', 'courses', {'unexpected': True})
            self.assertEqual(result['state'], 'invalid_arguments')
            load.assert_not_called()

    def test_finance_statuses_and_login_html(self):
        for status, headers, body, expected in [
            (401, {}, '', 'authentication_required'), (403, {}, '', 'access_denied'),
            (302, {'location': 'https://id.ustc.edu.cn/cas/login'}, '', 'authentication_required'),
            (302, {'location': 'https://other.example/login'}, '', 'unavailable'),
            (200, {}, '<input type="password">', 'authentication_required')]:
            transport = httpx.MockTransport(lambda r: httpx.Response(status, headers=headers, text=body))
            with self.assertRaises(ServiceError) as caught:
                FinanceClient({'state': {'cookies': []}}, transport).services()
            self.assertEqual(caught.exception.code, expected)

    def test_young_redirect_stops_before_waiting_for_dashboard(self):
        from school_mcp.young.client import YoungClient
        page = MagicMock()
        page.url = 'https://id.ustc.edu.cn/cas/login'
        page.goto.return_value = None
        browser = MagicMock()
        browser.new_context.return_value.new_page.return_value = page
        with patch('school_mcp.young.client.load_session', return_value={'state': {}, 'user_agent': 'synthetic'}), \
             patch('school_mcp.young.client.sync_playwright'), \
             patch('school_mcp.young.client.chrome_browser') as launch:
            launch.return_value.__enter__.return_value = browser
            with self.assertRaises(ServiceError) as caught:
                YoungClient().home()
        self.assertEqual(caught.exception.code, 'authentication_required')
        page.get_by_text.assert_not_called()

    def test_policy_retains_machine_readable_retry_interval(self):
        from school_mcp.network import CooldownError, PolicyError
        error = policy_error(BBError, CooldownError(12))
        self.assertEqual(error.code, 'cooldown')
        self.assertGreaterEqual(error.retry_after_seconds, 12)
        self.assertEqual(policy_error(BBError, PolicyError('unavailable')).code, 'local_policy_unavailable')

    def test_lookalike_identity_is_not_a_login_trigger(self):
        for url in ['https://id.ustc.edu.cn.evil.example/', 'http://id.ustc.edu.cn/',
                    'https://id.ustc.edu.cn:444/', 'https://u@id.ustc.edu.cn/']:
            self.assertFalse(identity_redirect(url))


if __name__ == '__main__':
    unittest.main()
