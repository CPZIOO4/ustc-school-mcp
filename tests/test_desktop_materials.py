import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from school_mcp.desktop import artifacts, materials
from school_mcp.desktop.chat import ChatRunner, StopRequested
from school_mcp.desktop.storage import Data, write_private
from school_mcp.service_errors import ServiceError


@unittest.skipUnless(os.name == 'nt', 'Windows encrypted profile')
class MaterialTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ); env.start(); self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.data = Data(tmp.name)
        self.chat = self.data.new_chat()
        self.runner = ChatRunner(self.data, threading.Event())

    def upload(self, name='材料.txt', content='三个编号 A19 B28 C37'.encode()):
        path = self.data.profile / 'uploads' / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
        self.chat['allowed_files'].append(str(path.resolve()))
        return path

    def read(self, path, **kwargs):
        return asyncio.run(self.runner.dispatch(self.chat, 'school_local_read_file', {'path': str(path), **kwargs}))

    def test_selected_text_pagination_and_encoding(self):
        content = '中文课程编号 A19\n第二项 B28\n末项 C37'
        for encoding in ('utf-8-sig', 'utf-16', 'gb18030'):
            path = self.upload(encoding + '.txt', content.encode(encoding))
            first = self.read(path, limit=7)
            self.assertFalse(first['complete'])
            self.assertEqual(first['content'] + self.read(path, offset=first['next_offset'])['content'], content)
            self.assertTrue(first['untrusted_content'])

    def test_other_chat_and_private_root_rejected(self):
        path = self.upload()
        with self.assertRaises(ServiceError):
            materials.call(self.data, self.data.new_chat(), 'school_local_read_file', {'path': str(path)})
        secret = self.data.root / 'model-settings.dpapi'
        secret.write_bytes(b'private')
        # A corrupted allowlist alone cannot authorize application-private paths.
        self.chat['allowed_files'].append(str(secret))
        with self.assertRaises(ServiceError):
            self.read(secret)
        self.assertEqual(self.chat['tasks'], [])

    def test_arguments_are_validated_without_trusting_model(self):
        path = self.upload()
        for bad in ({'limit': 12001}, {'offset': -1}, {'offset': True}, {'limit': '5'}, {'command': 'anything'}):
            with self.subTest(bad=bad), self.assertRaises(ServiceError):
                self.read(path, **bad)
        with self.assertRaises(ServiceError):
            materials.call(self.data, self.chat, 'school_local_list_files', {'path': str(path)})

    def test_known_attachment_digest_is_rechecked(self):
        path = self.data.profile / 'attachments' / 'returned.txt'
        path.parent.mkdir()
        path.write_bytes(b'Original')
        artifacts.register(self.data, self.chat, 'school_mail_download_attachment', {
            'dispatched': True, 'tool': 'school_mail_download_attachment', 'result': {
                'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}})
        self.assertEqual(self.read(path)['content'], 'Original')
        path.write_bytes(b'Changed')
        with self.assertRaises(ValueError):
            self.read(path)

    def test_unsupported_binary_missing_and_large_are_explicit(self):
        path = self.upload('legacy.xls', b'not-text')
        self.assertEqual(self.read(path)['status'], 'unsupported_format')
        path = self.upload(content=b'\x00binary')
        self.assertEqual(self.read(path)['status'], 'unreadable_file')
        path.unlink()
        self.assertEqual(self.read(path)['status'], 'missing_file')
        path = self.upload(content=b'abcd')
        with patch.object(materials, 'MAX_BYTES', 3):
            self.assertEqual(self.read(path)['status'], 'file_too_large')
        with patch.object(materials, 'MAX_CHARS', 3):
            self.assertEqual(self.read(path)['status'], 'text_too_large')

    def test_docx_body_tables_no_external_resources_or_entities(self):
        path = self.upload('材料.docx')
        xml = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>课程实验</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>样本 A19</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'''
        with ZipFile(path, 'w') as archive:
            archive.writestr('word/document.xml', xml)
            archive.writestr('word/_rels/document.xml.rels', 'https://private.example/do-not-fetch')
        result = self.read(path)
        self.assertIn('课程实验', result['content'])
        self.assertIn('样本 A19', result['content'])
        self.assertNotIn('private.example', result['content'])
        for encoding in ('utf-8', 'utf-16'):
            with ZipFile(path, 'w') as archive:
                archive.writestr('word/document.xml', ('<!DOCTYPE d [<!ENTITY x "expand">]>' + xml).encode(encoding))
            self.assertEqual(self.read(path)['status'], 'unreadable_file')

    def test_cancelled_turn_cannot_read_materials(self):
        path = self.upload()
        self.runner.stopped.set()
        with self.assertRaises(StopRequested):
            self.read(path)

    def test_model_loop_reads_file_and_receives_content_without_school_task(self):
        path = self.upload()
        write_private(self.data.settings_path, {'base_url': 'https://model.example/v1', 'model': 'synthetic',
                                              'api_key': 'synthetic-key', 'tested': True})
        self.data.save_chat(self.chat)
        requests = []
        def complete(client, messages, tools):
            requests.append(messages)
            self.assertIn('school_local_read_file', {t['function']['name'] for t in tools})
            if len(requests) == 1:
                return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'read_one', 'type': 'function',
                        'function': {'name': 'school_local_read_file', 'arguments': json.dumps({'path': str(path)})}}]}
            result = json.loads(messages[-1]['content'])
            self.assertEqual(result['content'], '三个编号 A19 B28 C37')
            return {'role': 'assistant', 'content': result['content']}
        with patch('school_mcp.desktop.model.ModelClient.complete', complete), patch('school_mcp.desktop.chat.mcp.call_tool') as remote:
            self.runner.run(self.chat['id'], '读取材料', files=[str(path)])
            remote.assert_not_called()
        saved = self.data.chat(self.chat['id'])
        self.assertEqual(saved['state'], 'idle')
        self.assertEqual(saved['tasks'], [])
        self.assertEqual(saved['messages'][-1]['content'], '三个编号 A19 B28 C37')


if __name__ == '__main__':
    unittest.main()
