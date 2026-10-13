import asyncio
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import httpx
from school_mcp.desktop import artifacts
from school_mcp.desktop.app import Application
from school_mcp.desktop.chat import ChatRunner, ui_view
from school_mcp.desktop.server import Server
from school_mcp.desktop.auth import save_verified_service
from school_mcp.bb.identity import save_session_identity
from school_mcp.young import registration as young


@unittest.skipUnless(os.name == 'nt', 'Windows encrypted profile')
class BusinessBridgeTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ); env.start(); self.addCleanup(env.stop)
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.app = Application(temp.name)
        self.data = self.app.data
        save_session_identity('synthetic-account')
        self.chat = self.data.new_chat()

    def output(self, path=None):
        path = path or self.data.profile / 'attachments' / 'example.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic artifact')
        return {'dispatched': True, 'tool': 'school_mail_download_attachment', 'result': {
            'path': str(path), 'filename': '<unsafe>.txt', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}

    def test_files_registered_only_by_known_tool_and_return(self):
        response = self.output()
        artifacts.register(self.data, self.chat, 'school_mail_read', response)
        self.assertFalse(self.chat.get('artifacts'))
        artifacts.register(self.data, self.chat, 'school_mail_download_attachment', response)
        self.data.save_chat(self.chat)
        record = self.chat['artifacts'][0]
        self.assertEqual(artifacts.download(self.data, self.chat['id'], record['id']), b'synthetic artifact')
        self.assertNotIn('path', ui_view(self.chat)['artifacts'][0])
        runner = ChatRunner(self.data, threading.Event())
        runner.file_check(self.chat, {'files': [record['path']]})
        Path(record['path']).write_bytes(b'changed')
        with self.assertRaises(ValueError):
            runner.file_check(self.chat, {'files': [record['path']]})
        with self.assertRaises(ValueError):
            artifacts.download(self.data, self.chat['id'], record['id'])

    def test_credentials_and_other_chat_files_cannot_be_downloaded(self):
        response = self.output(self.data.root / 'model-settings.dpapi')
        with self.assertRaises(ValueError):
            artifacts.register(self.data, self.chat, 'school_mail_download_attachment', response)
        self.assertFalse(self.chat.get('artifacts'))
        artifacts.register(self.data, self.chat, 'school_mail_download_attachment', self.output())
        self.data.save_chat(self.chat)
        other = self.data.new_chat()
        with self.assertRaises(ValueError):
            artifacts.download(self.data, other['id'], self.chat['artifacts'][0]['id'])

    def test_authenticated_download_http_and_locked_rejection(self):
        artifacts.register(self.data, self.chat, 'school_mail_download_attachment', self.output())
        self.data.save_chat(self.chat)
        with Server(self.app) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                url = server.origin + '/api/artifact?chat=' + self.chat['id'] + '&id=' + self.chat['artifacts'][0]['id']
                with httpx.Client(trust_env=False) as client:
                    self.assertEqual(client.get(url).status_code, 403)
                    result = client.get(url, headers={'X-School-Token': server.token})
                    self.assertEqual(result.content, b'synthetic artifact')
                    self.assertEqual(result.headers['Content-Type'], 'application/octet-stream')
                    (self.data.root / 'locked').touch()
                    self.assertNotEqual(client.get(url, headers={'X-School-Token': server.token}).status_code, 200)
            finally:
                server.shutdown(); thread.join()

    def test_valid_site_login_reuses_session_without_window(self):
        with patch.object(self.app, 'ensure_service', return_value={'connected': True}), patch('school_mcp.desktop.auth.manual_service') as manual:
            self.app.login('jw'); self.app.worker.join(5)
            manual.assert_not_called()
            self.assertTrue(self.app.operation['result']['session_reused'])

    def test_expired_site_login_opens_only_requested_site(self):
        with patch.object(self.app, 'ensure_service', return_value={'error_code': 'authentication_required'}), patch('school_mcp.desktop.auth.manual_service', return_value={'connected': True}) as manual:
            self.app.login('young'); self.app.worker.join(5)
            self.assertEqual(manual.call_args.args[0], 'young')
            self.assertEqual(self.app.connections['young']['state'], 'connected')

    def test_network_failure_never_causes_relogin(self):
        with patch.object(self.app, 'ensure_service', return_value={'error_code': 'cooldown'}), patch('school_mcp.desktop.auth.manual_service') as manual:
            self.app.login('jw'); self.app.worker.join(5)
            manual.assert_not_called()
            self.assertEqual(self.app.operation['state'], 'attention')

    def test_no_site_save_from_identity_page(self):
        module, context = Mock(), Mock()
        module.HOST = 'jw.ustc.edu.cn'
        result = save_verified_service('jw', module, context, Mock(url='https://id.ustc.edu.cn/cas/login'), 'synthetic', 'agent')
        self.assertFalse(result)
        module.save_session.assert_not_called()

    def test_library_login_can_finish_on_current_loans_page(self):
        module, context = Mock(), Mock()
        module.HOST = 'opac.lib.ustc.edu.cn'
        with patch('school_mcp.library.client.LibraryClient') as client:
            client.return_value.check.return_value = {'connected': True}
            self.assertTrue(save_verified_service('library', module, context,
                Mock(url='http://opac.lib.ustc.edu.cn/reader/book_lst.php'), 'synthetic', 'agent'))
            module.save_session.assert_called_once()

    def test_bound_mail_never_creates_another_password(self):
        with patch('school_mcp.desktop.app.mail_status', return_value={'configured': True}), patch.object(self.app, 'ensure_service', return_value={'connected': True}), patch('school_mcp.mail.browser_setup.run') as bind:
            self.app.login('mail'); self.app.worker.join(5)
            bind.assert_not_called()
            self.assertTrue(self.app.operation['result']['session_reused'])

    def test_new_capabilities_available_to_agent(self):
        from school_mcp.agent.catalog import describe
        for flow, name in [('academic', 'school_jw_course_schedule'), ('young', 'school_young_read_project')]:
            result = asyncio.run(describe(flow, name))
            self.assertEqual(result['tools'][0]['name'], name)


class YoungDetailsTests(unittest.TestCase):
    def test_minimal_plaintext_view(self):
        result = young.project_view({'id': 'one', 'baseContent': '<p>正文</p><script>secret()</script>', 'tel': 'private', 'uuidKey': 'private'})
        self.assertEqual(result['baseContent'], '正文')
        self.assertNotIn('tel', result)
        self.assertNotIn('uuidKey', result)

    def test_parent_and_child_membership_checked(self):
        with patch.object(young, 'load_session', return_value={}), patch.object(young, 'RegistrationBrowser') as factory:
            browser = factory.return_value.open.return_value.__enter__.return_value
            browser.listing_page.return_value = {'projects': [{'id': 'parent'}]}
            browser.detail.side_effect = [{'id': 'parent', 'itemCategory': '1'}, {'id': 'child'}]
            browser.children.return_value = [{'id': 'child'}]
            result = young.read_project('parent', child_id='child')
            self.assertEqual(result['project']['id'], 'child')
            self.assertFalse(result['registration_executed'])
            browser.submit.assert_not_called()
            with self.assertRaises(young.YoungError):
                young.read_project('other')
            browser.detail.side_effect = [{'id': 'parent', 'itemCategory': '1'}]
            with self.assertRaises(young.YoungError):
                young.read_project('parent', child_id='unrelated')


if __name__ == '__main__':
    unittest.main()
