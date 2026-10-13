"""Actual Rust app IO through private pipes; loopback synthetic provider only."""
import base64
from contextlib import closing, nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from school_mcp.desktop import native_core, model, school_model
from school_mcp.desktop.app import Application
from school_mcp.desktop.app_native import call
from school_mcp.desktop.chat import ChatRunner
from school_mcp.desktop.storage import Data, read_private, write_private

CORE = Path(os.environ.get('SCHOOL_TEST_CORE', Path(__file__).resolve().parents[1] / 'native/core/target/debug/school-core.exe'))


class ModelImportFinalizationTests(unittest.TestCase):
    """No filesystem/provider IO: cover the boundary after a successful test."""
    def setUp(self):
        self.key_id = '00000000-0000-0000-0000-000000000001'
        self.data = SimpleNamespace(root=Path('E:/synthetic-not-created'))
        session = SimpleNamespace(get=lambda _: SimpleNamespace(json=lambda: {'secret_key': 'synthetic-only'}))
        for replacement in [
            patch.object(school_model, 'browser_session', return_value=nullcontext(session)),
            patch.object(school_model, 'connect', return_value=[{'id': self.key_id, 'status': 'active'}]),
            patch.object(school_model, 'list_models', return_value={'models': ['synthetic']}),
            patch.object(school_model, 'save_settings'),
            patch.object(native_core, 'enabled', return_value=True),
        ]:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.incident_patch = patch('school_mcp.safety_log.record',
            return_value={'error_id': 'ERR-0000000000000001', 'diagnostic_saved': True})
        self.incident = self.incident_patch.start()
        self.addCleanup(self.incident_patch.stop)

    def test_stop_after_model_reply_prevents_replacing_primary_configuration(self):
        stop = threading.Event()
        def finished(*args):
            stop.set()
            return {'connected': True}
        with patch.object(school_model, 'test_connection', side_effect=finished), \
             patch('school_mcp.desktop.app_native.call', return_value={}) as native:
            result = school_model.import_key(self.data, self.key_id, 'synthetic', stopped=stop)
        self.assertEqual(result['state'], 'stopped')
        self.assertFalse(result['connected'])
        self.assertEqual([c.args[1] for c in native.call_args_list], ['discard_candidate'])

    def test_cleanup_failure_preserves_original_provider_error(self):
        primary = model.ModelError('model_authentication', 'synthetic authentication failure')
        cleanup = model.ModelError('storage_failure', 'synthetic cleanup failure')
        with patch.object(school_model, 'test_connection', side_effect=primary), \
             patch('school_mcp.desktop.app_native.call', side_effect=cleanup):
            with self.assertRaises(model.ModelError) as error:
                school_model.import_key(self.data, self.key_id, 'synthetic')
        self.assertIs(error.exception, primary)
        self.assertEqual(self.incident.call_args.kwargs['code'], 'storage_failure')
        self.assertEqual(self.incident.call_args.kwargs['stage'], 'persistence')

    def test_cleanup_failure_after_commit_reports_saved_result_without_retry(self):
        def native(_data, action):
            if action == 'discard_candidate':
                raise model.ModelError('storage_failure', 'synthetic cleanup failure')
            return {}
        with patch.object(school_model, 'test_connection', return_value={'connected': True}), \
             patch('school_mcp.desktop.app_native.call', side_effect=native) as calls:
            result = school_model.import_key(self.data, self.key_id, 'synthetic')
        self.assertTrue(result['connected'])
        self.assertTrue(result['candidate_cleanup_pending'])
        self.assertEqual(result['provider'], 'ustc')
        self.assertIn('无需重复导入', result['note'])
        self.assertEqual([c.args[1] for c in calls.call_args_list], ['commit_candidate', 'discard_candidate'])


class Provider(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.requests.append(('GET', self.path, None))
        self.reply({'data': [{'id': 'synthetic'}, {'id': 'synthetic'}, {'id': 'invalid name'}]})

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append(('POST', self.path, request))
        if self.server.pause:
            self.server.received.set()
            self.server.release.wait(10)
        if self.server.status != 200:
            return self.reply({'secret': 'synthetic-provider-body-never-exposed'}, self.server.status)
        second = any(m['role'] == 'tool' for m in request['messages'])
        if second:
            code = json.loads(request['messages'][-1]['content'])['verification_code']
            message = {'role': 'assistant', 'content': 'wrong' if self.server.bad_second else code}
        elif self.server.bad_first:
            message = {'role': 'assistant', 'content': 'No tools.'}
        else:
            message = {'role': 'assistant', 'tool_calls': [{'id': 'synthetic-one', 'type': 'function',
                'function': {'name': 'connection_check', 'arguments': '{"ok":true}'}}]}
        self.reply({'choices': [{'message': message}]})

    def reply(self, value, status=200):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # Expected when Rust drops a cancelled in-flight read.


@unittest.skipUnless(os.name == 'nt' and CORE.is_file(), 'Windows native core required')
class RustAppIOTests(unittest.TestCase):
    def setUp(self):
        native_core.shutdown()
        env = patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': str(CORE)})
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(native_core.shutdown)
        self.app = Application(Path(self.tmp.name) / 'app')
        self.data = self.app.data
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        self.server.daemon_threads = True
        self.server.requests = []
        self.server.bad_first = self.server.bad_second = self.server.pause = False
        self.server.status = 200
        self.server.received, self.server.release = threading.Event(), threading.Event()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.release.set)
        self.config = {'base_url': f'http://127.0.0.1:{self.server.server_port}/v1',
                       'model': 'synthetic', 'api_key': 'synthetic-test-key-only'}

    def configured(self):
        return model.save_settings(self.data, self.config)

    def async_test(self, data=None, stopped=None):
        results, errors = [], []
        def run():
            try:
                results.append(model.test_connection(data or self.data, stopped=stopped))
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        return thread, results, errors

    def begin_upload(self, raw=b'abc', name='材料.txt', digest=None):
        return call(self.data, 'upload_begin', name=name, size=len(raw),
                    sha256=digest or hashlib.sha256(raw).hexdigest())['id']

    def chunk(self, identifier, raw, offset=0):
        return call(self.data, 'upload_chunk', id=identifier, offset=offset,
                    content=base64.b64encode(raw).decode())

    def test_native_settings_and_model_list_do_not_read_secret_in_python(self):
        with patch('school_mcp.desktop.model.read_private', side_effect=AssertionError('legacy settings read')), \
             patch('school_mcp.desktop.model.write_private', side_effect=AssertionError('legacy settings write')), \
             patch('httpx.Client', side_effect=AssertionError('legacy HTTP')):
            saved = self.configured()
            self.assertFalse(saved['tested'])
            self.assertNotIn('api_key', saved)
            self.assertNotIn(self.config['api_key'], json.dumps(model.public_settings(self.data)))
            listed = model.list_models(self.data, {**self.config, 'api_key': ''})
            self.assertEqual(listed['models'], ['synthetic'])
        self.assertNotIn(self.config['api_key'].encode(), self.data.settings_path.read_bytes())

    def test_reused_key_stays_bound_to_normalized_address(self):
        self.configured()
        model.save_settings(self.data, {**self.config, 'base_url': self.config['base_url'] + '/chat/completions/', 'api_key': ''})
        self.assertEqual(read_private(self.data.settings_path)['api_key'], self.config['api_key'])
        with self.assertRaises(model.ModelError):
            model.save_settings(self.data, {**self.config, 'base_url': 'https://other.example/v1', 'api_key': ''})
        self.assertEqual(model.public_settings(self.data)['base_url'], self.config['base_url'])

    def test_full_model_roundtrip_and_commit_are_rust(self):
        self.configured()
        with patch.object(model.ModelClient, 'complete', side_effect=AssertionError('legacy model driver')), \
             patch('school_mcp.desktop.model.read_private', side_effect=AssertionError('legacy secret read')), \
             patch('httpx.Client', side_effect=AssertionError('legacy HTTP')):
            result = model.test_connection(self.data)
        self.assertTrue(result['tool_result_read'])
        self.assertFalse(result['school_data_sent'])
        self.assertEqual(len(self.server.requests), 2)
        self.assertTrue(model.public_settings(self.data)['tested'])
        self.assertNotIn('api_key', json.dumps(result))
        self.assertNotIn(str(self.data.root), json.dumps(self.server.requests))

    def test_invalid_first_and_second_turns_do_not_leave_historical_success(self):
        self.configured()
        self.assertTrue(model.test_connection(self.data)['connected'])
        for leg in ('bad_first', 'bad_second'):
            setattr(self.server, leg, True)
            before = len(self.server.requests)
            with self.assertRaises(model.ModelError) as error:
                model.test_connection(self.data)
            self.assertEqual(error.exception.code, 'model_tool_roundtrip')
            self.assertEqual(len(self.server.requests) - before, 1 if leg == 'bad_first' else 2)
            self.assertFalse(model.public_settings(self.data)['tested'])
            setattr(self.server, leg, False)

    def test_provider_error_is_not_retried_or_disclosed(self):
        self.configured()
        self.server.status = 401
        with self.assertRaises(model.ModelError) as error:
            model.test_connection(self.data)
        self.assertEqual(error.exception.code, 'model_authentication')
        self.assertNotIn('synthetic-provider-body', str(error.exception))
        self.assertEqual(len(self.server.requests), 1)
        self.assertFalse(model.public_settings(self.data)['tested'])

    def test_stop_before_start_sends_no_request(self):
        self.configured()
        stop = threading.Event()
        stop.set()
        self.assertEqual(model.test_connection(self.data, stopped=stop)['state'], 'stopped')
        self.assertEqual(self.server.requests, [])

    def test_stop_while_provider_waits_unblocks_without_second_request(self):
        self.configured()
        self.server.pause = True
        stop = threading.Event()
        thread, results, errors = self.async_test(stopped=stop)
        try:
            self.assertTrue(self.server.received.wait(5))
            self.assertFalse(model.public_settings(self.data)['tested'], 'local pipe must stay responsive')
            stop.set()
            thread.join(5)
            self.assertFalse(thread.is_alive(), 'cancellation waited for provider HTTP timeout')
            self.assertEqual(errors, [])
            self.assertEqual(results[0]['state'], 'stopped')
            self.assertEqual(len(self.server.requests), 1)
            self.assertFalse(model.public_settings(self.data)['tested'])
        finally:
            self.server.release.set()
            thread.join(10)

    def test_real_application_connect_and_stop_entry_preserves_operation_record(self):
        self.server.pause = True
        started = self.app.api('/api/model/connect', self.config)
        self.assertTrue(started['started'])
        try:
            self.assertTrue(self.server.received.wait(5))
            self.assertTrue(self.app.api('/api/stop', {})['stop_requested'])
            self.app.worker.join(5)
            self.assertFalse(self.app.busy())
            self.assertEqual(self.app.operation['state'], 'stopped')
            saved = read_private(self.data.root / 'operation.dpapi')
            self.assertEqual(saved['id'], started['operation_id'])
            self.assertEqual(saved['state'], 'stopped')
            self.assertEqual(len(self.server.requests), 1)
            self.assertNotIn(self.config['api_key'], json.dumps(saved))
        finally:
            self.server.release.set()
            self.app.worker.join(10)

    def test_setting_changed_during_test_never_receives_stale_green_badge(self):
        self.configured()
        self.server.pause = True
        thread, results, errors = self.async_test()
        try:
            self.assertTrue(self.server.received.wait(5))
            model.save_settings(self.data, {**self.config, 'model': 'changed-model'})
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, [])
            self.assertEqual(errors[0].code, 'model_settings_changed')
            self.assertEqual(model.public_settings(self.data)['model'], 'changed-model')
            self.assertFalse(model.public_settings(self.data)['tested'])
            self.assertEqual(len(self.server.requests), 1)
        finally:
            self.server.release.set()
            thread.join(10)

    def test_completed_test_token_cannot_execute_again(self):
        self.configured()
        token = call(self.data, 'test_begin')['id']
        call(self.data, 'test_connection', id=token)
        with self.assertRaises(model.ModelError):
            call(self.data, 'test_connection', id=token)
        self.assertEqual(len(self.server.requests), 2)

    def test_candidate_import_does_not_replace_primary_on_failure(self):
        self.configured()
        candidate = SimpleNamespace(root=self.data.root, model_slot='candidate')
        model.save_settings(candidate, {**self.config, 'model': 'candidate'})
        self.server.bad_second = True
        with self.assertRaises(model.ModelError):
            model.test_connection(candidate)
        with self.assertRaises(model.ModelError):
            call(candidate, 'commit_candidate')
        self.assertEqual(model.public_settings(self.data)['model'], 'synthetic')
        self.server.bad_second = False
        self.assertTrue(model.test_connection(candidate)['connected'])
        call(candidate, 'commit_candidate')
        self.assertEqual(model.public_settings(self.data)['model'], 'candidate')
        self.assertTrue(model.public_settings(self.data)['tested'])
        call(candidate, 'discard_candidate')
        self.assertFalse((self.data.root / 'school-model-candidate.dpapi').exists())

    def test_root_initialization_rejects_unrelated_data(self):
        other = Path(self.tmp.name) / 'other'
        other.mkdir()
        keep = other / 'keep.txt'
        keep.write_bytes(b'unrelated')
        with self.assertRaises(model.ModelError):
            Data(other)
        self.assertEqual(keep.read_bytes(), b'unrelated')
        self.assertFalse((other / 'school-desktop.json').exists())

    def test_upload_beyond_pipe_frame_limit_is_written_only_by_rust(self):
        raw = b's' * (50 * 1024 * 1024)
        self.assertGreater(len(raw), native_core.MAX_FRAME)
        with patch.object(self.app, 'require_login'), \
             patch.object(Path, 'write_bytes', side_effect=AssertionError('legacy upload write')):
            value = self.app.upload('大材料.bin', raw)
        self.assertEqual(value['size'], len(raw))
        self.assertEqual(Path(value['path']).read_bytes(), raw)
        self.assertEqual(ChatRunner.validate_files(self.data, [value['path']]), [value['path']])

    def test_upload_size_and_name_limits(self):
        for name in ['../bad.txt', 'CON.txt', 'trailing.', 'x/y', 'x\\y', '', '.upload.part']:
            with self.subTest(name=name):
                with self.assertRaises(model.ModelError):
                    self.begin_upload(name=name)
        for size in [0, 50 * 1024 * 1024 + 1, -1, True]:
            with self.assertRaises(model.ModelError):
                call(self.data, 'upload_begin', name='file.bin', size=size, sha256='a' * 64)
        token = call(self.data, 'upload_begin', name='largest.bin', size=50 * 1024 * 1024, sha256='a' * 64)['id']
        self.assertTrue(call(self.data, 'upload_abort', id=token)['aborted'])

    def test_wrong_chunk_offset_and_hash_do_not_publish_or_retry_bytes(self):
        token = self.begin_upload(digest='0' * 64)
        self.chunk(token, b'abc')
        with self.assertRaises(model.ModelError):
            self.chunk(token, b'abc')
        with self.assertRaises(model.ModelError):
            call(self.data, 'upload_finish', id=token)
        directory = self.data.profile / 'uploads' / token
        self.assertEqual((directory / '.upload.part').read_bytes(), b'abc')
        self.assertFalse((directory / '材料.txt').exists())
        self.assertTrue(call(self.data, 'upload_abort', id=token)['aborted'])
        self.assertFalse(directory.exists())

    def test_incomplete_upload_and_abort_cannot_delete_published_file(self):
        token = self.begin_upload()
        with self.assertRaises(model.ModelError):
            call(self.data, 'upload_finish', id=token)
        self.chunk(token, b'abc')
        value = call(self.data, 'upload_finish', id=token)
        self.assertFalse(call(self.data, 'upload_abort', id=token)['aborted'])
        self.assertEqual(Path(value['path']).read_bytes(), b'abc')
        with self.assertRaises(model.ModelError):
            self.chunk(token, b'extra', 3)

    def test_file_validation_rejects_outside_and_partial_uploads(self):
        outside = Path(self.tmp.name) / 'outside.txt'
        outside.write_bytes(b'synthetic')
        token = self.begin_upload()
        partial = self.data.profile / 'uploads' / token / '.upload.part'
        for files in [[str(outside)], [str(partial)], [1], 'not-list', ['x'] * 11]:
            with self.assertRaises(model.ModelError):
                ChatRunner.validate_files(self.data, files)
        self.assertEqual(ChatRunner.validate_files(self.data, []), [])

    def test_legacy_files_remain_selectable_and_chats_recover_without_replay(self):
        with patch.object(native_core, 'enabled', return_value=False):
            chat = self.data.new_chat()
            chat.update(state='running', pending={'id': 'old-explicit-approval'},
                        messages=[{'role': 'user', 'content': 'synthetic task'},
                                  {'role': 'tool', 'tool_call_id': 'existing', 'content': '{"state":"unknown"}'}])
            self.data.save_chat(chat)
        with patch.object(Data, 'db', side_effect=AssertionError('legacy recovery DB')), \
             patch.object(Data, 'save_chat', side_effect=AssertionError('legacy recovery save')):
            self.data.recover()
        restored = self.data.chat(chat['id'])
        self.assertEqual(restored['state'], 'interrupted')
        self.assertEqual(restored['pending'], chat['pending'])
        self.assertEqual(restored['messages'], chat['messages'])
        self.assertTrue(restored['events'][-1]['detail']['diagnostic_saved'])
        before = len(restored['events'])
        self.data.recover()
        self.assertEqual(len(self.data.chat(chat['id'])['events']), before)
        uploads = self.data.profile / 'uploads'
        uploads.mkdir(exist_ok=True)
        legacy = uploads / 'legacy.txt'
        legacy.write_bytes(b'legacy synthetic')
        self.assertEqual(ChatRunner.validate_files(self.data, [str(legacy)]), [str(legacy)])

    def test_recovery_transaction_does_not_partially_update_on_corrupt_row(self):
        chat = self.data.new_chat()
        chat['state'] = 'running'
        self.data.save_chat(chat)
        with closing(sqlite3.connect(self.data.profile / 'desktop-chats.sqlite3')) as db:
            db.execute('INSERT INTO chats VALUES (?,?)', ('f' * 32, b'invalid-synthetic'))
            db.commit()
        with self.assertRaises(model.ModelError):
            call(self.data, 'recover', incident={})
        self.assertEqual(self.data.chat(chat['id'])['state'], 'running')
        self.assertEqual(self.data.chat(chat['id'])['events'], [])


if __name__ == '__main__':
    unittest.main()
