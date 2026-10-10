from contextlib import nullcontext
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from school_mcp.mail import browser_setup as setup
from school_mcp.mail.config import MailError

ADDRESS = 'synthetic-user@mail.ustc.edu.cn'
# Four groups of four synthetic characters, formatted like the school dialog.
SECRET = ''.join(('test', 'only', 'Pass', '1234'))
DIALOG = 'Specific password generated\nThe private password SchoolMCP\ntest only Pass 1234\nCopy\nEmail address\n' + ADDRESS + '\nIMAP 993\nSMTP 465'


class MailBrowserSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.environment = patch.dict(os.environ, {'SCHOOL_MCP_LOCAL_DIR': self.tmp.name, 'SCHOOL_MAIL_PASSWORD': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_parse_requires_generated_dialog_and_unique_address_and_secret(self):
        self.assertEqual(setup.parse_generated(DIALOG), (ADDRESS, SECRET))
        self.assertIsNone(setup.parse_generated(DIALOG.replace('Specific password generated', 'Inbox')))
        self.assertIsNone(setup.parse_generated(DIALOG + chr(10) + 'other@mail.ustc.edu.cn'))
        self.assertIsNone(setup.parse_generated(DIALOG + '\nonly test 4321 Pass'))
        self.assertIsNone(setup.parse_generated(DIALOG.replace('465', '25')))
        with self.assertRaises(MailError):
            setup.parse_generated(DIALOG, 'other@mail.ustc.edu.cn')

    def test_only_school_https_current_session_navigation(self):
        original = 'https://mail.ustc.edu.cn/coremail/XT/index.jsp?sid=synthetic-session'
        self.assertEqual(setup.settings_url(original), original + '#setting.security.altpwd')
        for bad in ('http://mail.ustc.edu.cn/coremail/XT/index.jsp',
                    'https://mail.ustc.edu.cn.evil.example/coremail/XT/index.jsp',
                    'https://synthetic-user@mail.ustc.edu.cn/coremail/XT/index.jsp',
                    'https://mail.ustc.edu.cn:8443/coremail/XT/index.jsp',
                    'https://mail.ustc.edu.cn/coremail/login.jsp'):
            self.assertIsNone(setup.settings_url(bad))

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_crashed_creation_cannot_be_replayed(self):
        setup._save({'state': 'creation_attempted', 'label': 'synthetic-label'})
        with patch('school_mcp.browser.chrome_browser') as browser, patch.object(setup, 'finish_pending') as finish:
            result = setup.run(headed=True, create_client_password=True, replace_existing=True)
        self.assertEqual(result['state'], 'creation_attempted')
        browser.assert_not_called()
        finish.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_pending_failure_recovery_is_encrypted_and_never_creates_again(self):
        pending = {'state': 'captured', 'address': ADDRESS, 'password': SECRET,
                   'previous_config': setup.config_fingerprint()}
        setup._save(pending)
        with patch.object(setup, 'connect_and_save', side_effect=MailError('synthetic network failure')):
            with self.assertRaises(MailError):
                setup.run(resume=True)
        self.assertEqual(setup.status()['state'], 'captured')
        self.assertNotIn(SECRET, json.dumps(setup.status()))
        self.assertNotIn(SECRET.encode(), (Path(self.tmp.name) / setup.JOURNAL).read_bytes())
        with patch.object(setup, 'connect_and_save', return_value={'connected': True}), patch('school_mcp.browser.chrome_browser') as browser:
            result = setup.run(resume=True)
        browser.assert_not_called()
        self.assertEqual(result['state'], 'connected')
        self.assertNotIn('password', setup._load())

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_changed_mailbox_cannot_be_overwritten_by_resume(self):
        pending = {'state': 'captured', 'address': ADDRESS, 'password': SECRET,
                   'previous_config': setup.config_fingerprint()}
        setup._save(pending)
        (Path(self.tmp.name) / 'mail.json').write_text(json.dumps({'address': 'other@mail.ustc.edu.cn'}))
        with patch.object(setup, 'connect_and_save') as save:
            with self.assertRaises(MailError):
                setup.run(resume=True)
        save.assert_not_called()
        self.assertEqual(setup.status()['state'], 'captured')

    def test_headed_creation_is_explicit_and_existing_configuration_is_kept(self):
        with patch.object(setup, 'bind_lock', return_value=nullcontext()), patch.object(setup, 'local_status', return_value={'configured': False}):
            with self.assertRaises(MailError):
                setup.run()
        with patch.object(setup, 'bind_lock', return_value=nullcontext()), patch.object(setup, 'local_status', return_value={'configured': True}):
            self.assertEqual(setup.run(headed=True, create_client_password=True)['state'], 'existing_configuration')

    def test_environment_secret_override_blocks_browser_binding(self):
        with patch.dict(os.environ, {'SCHOOL_MAIL_PASSWORD': 'synthetic-environment-secret'}):
            with self.assertRaises(MailError):
                setup.run(headed=True, create_client_password=True)

    @unittest.skipUnless(os.name == 'nt', 'Windows lock')
    def test_parallel_process_cannot_acquire_binding_lock(self):
        with setup.bind_lock():
            with self.assertRaises(MailError):
                with setup.bind_lock():
                    self.fail('second binding lock acquired')

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_ambiguous_browser_failure_keeps_manual_window_and_hides_exception(self):
        browser = Mock()
        page = browser.new_context.return_value.new_page.return_value
        page.is_closed.side_effect = [False, False, True]
        def failed(*args, **kwargs):
            setup._save({'state': 'creation_attempted'})
            raise RuntimeError(SECRET)
        notify = Mock()
        with patch('playwright.sync_api.sync_playwright', return_value=nullcontext(None)), \
             patch('school_mcp.browser.chrome_browser', return_value=nullcontext(browser)), \
             patch.object(setup, 'automate_page', side_effect=failed):
            result = setup.run(headed=True, create_client_password=True, notify=notify)
        self.assertEqual(result['state'], 'creation_attempted')
        self.assertTrue(result['requires_user_action'])
        self.assertEqual(result['manual_window_seconds'], 180)
        page.wait_for_timeout.assert_called_once_with(500)
        self.assertNotIn(SECRET, json.dumps(result))
        notify.assert_called_once()


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI and installed Chrome')
class MailBrowserFixtureTests(unittest.TestCase):
    def test_recognized_browser_flow_captures_before_validation(self):
        from playwright.sync_api import sync_playwright
        from school_mcp.browser import chrome_browser
        html = '''<html><body><main id="app"><button onclick="document.getElementById('form').hidden=false">Generate Specific Password</button>
        <section id="form" hidden><label>Password name<input type="text"></label><button onclick="fetch('/synthetic-generate',{method:'POST'}).then(()=>{document.getElementById('form').hidden=true;document.getElementById('result').hidden=false})">Generate</button></section>
        <section id="result" hidden><h2>Specific password generated</h2><p>The private password SchoolMCP</p><p>test only Pass 1234</p><button>Copy</button><p>Email address</p><p>synthetic-user@mail.ustc.edu.cn</p><p>IMAP 993</p><p>SMTP 465</p></section></main></body></html>'''
        attempts = []
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'SCHOOL_MCP_LOCAL_DIR': tmp, 'SCHOOL_MAIL_PASSWORD': ''}):
            with sync_playwright() as p, chrome_browser(p, headless=True) as browser:
                context = browser.new_context(proxy={'server': 'http://127.0.0.1:9'}, service_workers='block')
                page = context.new_page()
                def route(request):
                    path = request.request.url.split('?', 1)[0]
                    if path == setup.WEBMAIL:
                        request.fulfill(status=200, content_type='text/html', body="<script>location.replace('/coremail/XT/index.jsp?sid=synthetic')</script>")
                    elif path.endswith('/synthetic-generate'):
                        self.assertEqual(setup._load()['state'], 'creation_attempted')
                        attempts.append(request.request.method)
                        request.fulfill(status=200, body='{}')
                    elif '/coremail/XT/index.jsp' in path:
                        request.fulfill(status=200, content_type='text/html', body=html)
                    else:
                        request.abort()
                page.route('**/*', route)
                def check(address, secret):
                    self.assertEqual(setup._load()['state'], 'captured')
                    self.assertEqual((address, secret), (ADDRESS, SECRET))
                    return {'connected': True}
                with patch.object(setup, 'connect_and_save', side_effect=check):
                    result = setup.automate_page(page, expected_address=ADDRESS, timeout=30,
                                                 label='SchoolMCP-synthetic', notify=lambda _: None)
        self.assertEqual(attempts, ['POST'])
        self.assertTrue(result['imap_verified'])
        self.assertFalse(result['mail_sent'])
        self.assertNotIn(SECRET, json.dumps(result))


if __name__ == '__main__':
    unittest.main()
