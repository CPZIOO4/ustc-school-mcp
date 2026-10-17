"""Fixed login workflow with isolated Chrome pages and synthetic credentials only."""
from contextlib import contextmanager
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from school_mcp.bb import identity_login as flow
from school_mcp.bb.session import BBError

ADDRESS = 'synthetic-user@mail.ustc.edu.cn'
SUCCESS = '<h1>个人信息</h1><a>安全设置</a><a>访问历史</a>'
MASK = ADDRESS[:3] + '*' * (len(ADDRESS) - 7) + ADDRESS[-4:]
LOGIN = '''<input name="username"><input type="password">
<button onclick="document.body.innerHTML='<h2>二次身份验证</h2>';setTimeout(()=>{document.body.innerHTML=window.challenge},100)">登录</button>
<script>window.challenge=CHALLENGE;</script>'''
CHALLENGE = f'''<h2>二次身份验证</h2><button>邮件验证码</button>
<p>已绑定密保邮箱：{MASK}</p><a>如何绑定邮箱</a>
<input name="emailCode" placeholder="验证码">
<label><input type="checkbox">信任此设备</label>
<button onclick="window.requests=(window.requests||0)+1">获取验证码</button>
<button onclick="document.cookie='SyntheticSSO=accepted;path=/;Secure';location.href='/gate/cas-success/'">验证</button>'''


class IdentityFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = sync_playwright().start()
        try:
            cls.browser = cls.p.chromium.launch(channel='chrome', headless=True)
        except PlaywrightError:
            cls.p.stop()
            raise unittest.SkipTest('Installed Chrome required')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.p.stop()

    def setUp(self):
        self.saved = None
        self.reuse_works = True
        original_new_context = self.browser.new_context
        def new_context(**options):
            ctx = original_new_context(**options)
            def serve(route):
                import json
                req = route.request
                path = req.url.split('id.ustc.edu.cn', 1)[-1]
                has_cookie = 'SyntheticSSO=accepted' in req.all_headers().get('cookie', '')
                if path.startswith('/gate/cas-success/'):
                    route.fulfill(body=SUCCESS, content_type='text/html; charset=utf-8')
                elif has_cookie and self.reuse_works:
                    # Playwright only routes the first URL in an HTTP redirect
                    # chain. Script navigation keeps every request intercepted.
                    route.fulfill(body="<script>location.href='/gate/cas-success/'</script>", content_type='text/html')
                else:
                    route.fulfill(body=LOGIN.replace('CHALLENGE', json.dumps(CHALLENGE)), content_type='text/html; charset=utf-8')
            ctx.route('**/*', serve)
            return ctx
        self.new_context = patch.object(self.browser, 'new_context', side_effect=new_context)
        self.new_context.start()
        self.addCleanup(self.new_context.stop)
        @contextmanager
        def browser(_p, **_kwargs):
            yield self.browser
        @contextmanager
        def playwright():
            yield self.p
        def load(account):
            if self.saved is None:
                raise BBError('synthetic missing device')
            self.assertEqual(account, 'SYNTHETIC001')
            return self.saved
        def save(account, state, channel, options):
            self.saved = dict(account=account, state=state, channel=channel, context_options=options)
            return self.saved
        self.progress = Mock()
        patches = [
            patch.object(flow, 'sync_playwright', side_effect=playwright),
            patch.object(flow, 'chrome_browser', side_effect=browser),
            patch.object(flow, 'login_state', self.progress),
            patch.object(flow, 'load_device_state', side_effect=load),
            patch.object(flow, 'save_device_state', side_effect=save),
            patch.object(flow, 'load_credentials', return_value=dict(username='SYNTHETIC001', password='synthetic-only',
                email_verification_enabled=True, email_verification_address=ADDRESS)),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.mail = Mock(config=SimpleNamespace(address=ADDRESS))
        self.mail.verification_snapshot.return_value = dict(uid_validity=123, watermark_uid=97)
        message = dict(uid=101, uid_validity=123, subject='统一身份认证验证码',
            body='统一身份认证登录验证码：654321', **{'from':'id@ustc.edu.cn', 'to':ADDRESS})
        self.mail.verification_headers.return_value = dict(uid_validity=123, messages=[message])
        self.mail.read.return_value = message

    def run_login(self):
        with patch('school_mcp.bb.email_verification.MailClient', return_value=self.mail), \
             patch('school_mcp.bb.email_verification.load_config', return_value=self.mail.config):
            return flow.run(timeout_seconds=5)

    def test_password_mfa_persist_then_independent_context_reuse(self):
        result = self.run_login()
        self.assertTrue(result['connected'])
        self.assertTrue(result['saved_state_reused'])
        self.assertTrue(result['email_code_submitted'])
        self.assertEqual(result['credential_submission_count'], 1)
        self.mail.verification_snapshot.assert_called_once()
        self.assertNotIn('654321', repr(self.progress.mock_calls))
        self.assertNotIn('synthetic-only', repr(result))
        # A second call must be cookie-only, without password or email polling.
        with patch.object(flow, 'try_submit_credentials', side_effect=AssertionError('no password required')):
            self.assertEqual(self.run_login()['credential_submission_count'], 0)
        self.mail.verification_snapshot.assert_called_once()

    def test_failure_to_reuse_cannot_report_connected(self):
        self.reuse_works = False
        with patch.object(flow, 'wait_authenticated', side_effect=lambda page: flow.authenticated(page)):
            self.assertIsNone(self.run_login())
        self.assertNotIn('connected', [call.args[0] for call in self.progress.call_args_list])
        self.assertIn('waiting_for_verification', [call.args[0] for call in self.progress.call_args_list])

    def test_connection_probe_never_submits_credentials_or_requests_mail(self):
        self.assertTrue(self.run_login()['connected'])
        self.reuse_works = False
        with patch.object(flow, 'wait_authenticated', side_effect=lambda page: flow.authenticated(page)), \
             patch.object(flow, 'try_submit_credentials', side_effect=AssertionError('probe must not log in')):
            result = flow.check_connection()
        self.assertFalse(result['connected'])
        self.assertEqual(result['credential_submission_count'], 0)
        self.mail.verification_snapshot.assert_called_once()

    def test_success_text_on_wrong_page_does_not_prove_login(self):
        context = self.browser.new_context()
        try:
            page = context.new_page()
            page.goto(flow.ENTRY)
            page.set_content(SUCCESS)
            self.assertFalse(flow.authenticated(page))
        finally:
            context.close()


@unittest.skipUnless(os.name == 'nt', 'Coordinator storage uses Windows DPAPI')
class IdentityCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_schema_authorization_and_real_registered_dispatch(self):
        from school_mcp.agent import engine
        from school_mcp.agent.catalog import describe, entry_schema, route
        from school_mcp.agent.store import Store
        with tempfile.TemporaryDirectory() as private, patch.dict(os.environ, {'SCHOOL_MCP_LOCAL_DIR': private}):
            self.assertIn('identity', [item['workflow'] for item in route('统一身份登录')['candidates']])
            self.assertEqual(entry_schema('identity')['name'], 'school_bb_identity_status')
            schema = (await describe('identity', 'school_bb_identity_reconnect'))['tools'][0]['input_schema']
            self.assertEqual(schema['properties'], {})
            task = Store().create('identity', 'synthetic fixed identity login', None)
            with patch.object(flow, 'start', return_value={'started':True, 'headless':True}) as start:
                result = await engine.call(task['task_id'], 'school_bb_identity_reconnect', {})
                self.assertEqual(result['state'], 'authorization_required')
                start.assert_not_called()
                result = await engine.call(task['task_id'], 'school_bb_identity_reconnect', {}, authorized=True)
                self.assertTrue(result['dispatched'])
                self.assertTrue(result['result']['started'])
                start.assert_called_once_with()
                with self.assertRaises(ValueError):
                    await engine.call(task['task_id'], 'school_bb_identity_reconnect', {'url':'https://evil.example'}, authorized=True)


if __name__ == '__main__':
    unittest.main()
