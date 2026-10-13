"""Real Rust pipe/HTTP acceptance; synthetic data, no school or paid model calls."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from school_mcp.desktop import native_core, model, context

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get('SCHOOL_TEST_CORE', ROOT / 'native/core/target/debug/school-core.exe'))


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass  # Expected when a rejected/oversized response is discarded.
    def log_message(self, *_):
        pass
    def do_GET(self):
        self.respond()
    def do_POST(self):
        self.respond()
    def respond(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.requests.append((self.path, dict(self.headers), json.loads(raw) if raw else None, self.client_address))
        if getattr(self.server, 'pause', None):
            received, release = self.server.pause
            received.set()
            release.wait(10)
        status, body = self.server.response
        self.send_response(status)
        if self.server.redirect:
            self.send_header('Location', self.server.redirect)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(CORE.is_file(), 'build native/core first')
class RustCoreTests(unittest.TestCase):
    def setUp(self):
        native_core.shutdown()
        env = patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': str(CORE)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(native_core.shutdown)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.requests = []
        self.server.redirect = None
        self.reply({'choices':[{'message':{'role':'assistant','content':'synthetic OK'}}]})
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.settings = {'base_url': f'http://127.0.0.1:{self.server.server_port}/v1',
                         'model': 'synthetic', 'api_key': 'synthetic-key-only'}
    def reply(self, value, status=200):
        self.server.response = (status, json.dumps(value, ensure_ascii=False).encode())
    def complete(self):
        return model.ModelClient(self.settings).complete([{'role':'user','content':'synthetic'}], [])

    def test_native_is_executing_and_http_pool_is_reused(self):
        self.assertEqual(native_core.call('status', {})['implementation'], 'rust')
        with patch('httpx.Client', side_effect=AssertionError('Python HTTP must not execute')):
            self.assertEqual(self.complete()['content'], 'synthetic OK')
            self.complete()
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(self.server.requests[0][3], self.server.requests[1][3])
        self.assertEqual(self.server.requests[0][1]['authorization'], 'Bearer synthetic-key-only')

    def test_waiting_model_does_not_block_local_storage(self):
        from school_mcp.desktop.storage import write_private, read_private
        received, release, saved = threading.Event(), threading.Event(), threading.Event()
        self.server.pause = (received, release)
        errors = []
        def request():
            try:
                self.complete()
            except Exception as error:
                errors.append(type(error).__name__)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'state.dpapi'
            def save():
                try:
                    write_private(path, {'state':'synthetic-local-update'})
                    saved.set()
                except Exception as error:
                    errors.append(type(error).__name__)
            network = threading.Thread(target=request)
            network.start()
            writer = threading.Thread(target=save)
            try:
                self.assertTrue(received.wait(5))
                writer.start()
                self.assertTrue(saved.wait(5), 'storage waited for provider response')
                self.assertEqual(read_private(path), {'state':'synthetic-local-update'})
                self.assertTrue(network.is_alive())
            finally:
                release.set()
                network.join(10)
                if writer.ident is not None:
                    writer.join(10)
            self.assertEqual(errors, [])

    def test_redirect_not_followed_and_failure_not_retried(self):
        for status, code in [(401,'model_authentication'),(429,'model_rate_limited'),(500,'model_http_error'),(307,'model_http_error')]:
            self.server.redirect = self.settings['base_url'] + '/should-not-follow'
            self.reply({'secret': 'synthetic-must-not-appear'}, status)
            before = len(self.server.requests)
            with self.assertRaises(model.ModelError) as error:
                self.complete()
            self.assertEqual(error.exception.code, code)
            self.assertNotIn('must-not-appear', str(error.exception))
            self.assertEqual(len(self.server.requests), before+1)

    def test_model_list_deduplicates_and_filters_invalid_ids(self):
        self.reply({'data':[{'id':'one'}, {'id':'one'}, {'id':'bad name'}, {'id':'two'}]})
        result = native_core.call('model_list', {'settings': self.settings})
        self.assertEqual(result['models'], ['one','two'])
        self.reply({}, 404)
        self.assertFalse(native_core.call('model_list', {'settings': self.settings})['available'])

    def test_late_malformed_call_invalidates_whole_response(self):
        calls = [{'id':name, 'type':'function','function':{'name':'submit','arguments':args}}
                 for name,args in [('a','{}'),('b','{"x":1,"x":2}')]]
        self.reply({'choices':[{'message':{'role':'assistant','tool_calls':calls}}]})
        with self.assertRaises(model.ModelError) as error:
            self.complete()
        self.assertEqual(error.exception.code, 'invalid_tool_arguments')
        self.reply({'choices':[{'finish_reason':'length','message':{'role':'assistant','tool_calls':calls[:1]}}]})
        with self.assertRaises(model.ModelError) as error:
            self.complete()
        self.assertEqual(error.exception.code, 'incomplete_model_response')

    def test_bounds_prevent_network_and_reject_large_response(self):
        with self.assertRaises(model.ModelError) as error:
            model.ModelClient(self.settings).complete([{'role':'user','content':'x'*900001}], [])
        self.assertEqual(error.exception.code, 'context_capacity')
        self.assertEqual(self.server.requests, [])
        self.reply({'data':'x'*(2*1024*1024+1)})
        with self.assertRaises(model.ModelError) as error:
            self.complete()
        self.assertEqual(error.exception.code, 'invalid_model_response')

    def test_closed_worker_stops_call_without_falling_back(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': str(Path(tmp)/'missing.exe')}):
            with patch('httpx.Client', side_effect=AssertionError('no fallback')):
                with self.assertRaises(model.ModelError) as error:
                    self.complete()
            self.assertEqual(error.exception.code, 'runtime_failure')
        self.assertEqual(self.server.requests, [])

    def test_context_retains_exact_constraints_and_receipts(self):
        messages = [{'role':'user','content':'原要求：不提交'}, {'role':'assistant','content':'x'*3000},
                    {'role':'user','content':'更正：只准备我的文件'}]
        chat = {'messages':messages, 'tasks':[], 'allowed_files':[]}
        memory = {'receipt':'exact-unknown-result', 'authorized':False}
        with patch.object(context, 'task_memory', return_value=memory):
            projected, report = context.project(chat, 'instructions', [], 'model', budget=1000)
        self.assertTrue(report['compacted'])
        self.assertEqual([m for m in projected if m['role']=='user'], [messages[0],messages[2]])
        self.assertEqual(json.loads(projected[1]['content']), memory)
        self.assertEqual(chat['messages'], messages)

    def test_validation_contract_matches_legacy(self):
        good = [{'role':'assistant','content':'好的', 'reasoning_content':'a', 'extra':'ignored'},
                {'role':'assistant','tool_calls':[{'id':'a','type':'function','function':{'name':'read','arguments':'{"x":true}'}}]}]
        bad = [{'role':'assistant','content':[]}, {'role':'assistant','content':''}, {'role':'user','content':'x'},
               {'role':'assistant','refusal':'no'}, {'role':'assistant','tool_calls':{}}]
        for value in good+bad:
            results=[]
            for native in (False, True):
                with patch.object(native_core, 'enabled', return_value=native):
                    try:
                        results.append(('ok',model.validated_message(value)))
                    except model.ModelError as error:
                        results.append(('error',error.code))
            self.assertEqual(results[0],results[1])

    def test_encrypted_files_interoperate_both_directions_and_no_plaintext(self):
        from school_mcp.desktop.storage import write_private, read_private
        value = {'test': '合成敏感内容', 'flag': True, 'number': 9007199254740993}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'test.dpapi'
            with patch.object(native_core, 'enabled', return_value=False):
                write_private(path, value)
            self.assertEqual(read_private(path), value)
            write_private(path, {'updated':value})
            with patch.object(native_core, 'enabled', return_value=False):
                self.assertEqual(read_private(path), {'updated':value})
            self.assertNotIn('合成敏感内容'.encode(), path.read_bytes())
            self.assertEqual(list(Path(tmp).glob('*.tmp')), [])
            path.write_bytes(b'corrupt-synthetic-data')
            with self.assertRaises(model.ModelError) as error:
                read_private(path)
            self.assertEqual(error.exception.code, 'storage_failure')

    def test_chat_database_remains_readable_to_previous_application(self):
        from school_mcp.desktop.storage import Data
        with tempfile.TemporaryDirectory() as tmp:
            data = Data(tmp)
            with patch.object(native_core, 'enabled', return_value=False):
                chat = data.new_chat()
            self.assertEqual(data.chat(chat['id']), chat)
            chat.update(state='running', messages=[{'role':'user','content':'synthetic request'}])
            data.save_chat(chat)
            with patch.object(native_core, 'enabled', return_value=False):
                self.assertEqual(data.chat(chat['id']), chat)
            data.recover()
            self.assertEqual(data.chat(chat['id'])['state'], 'interrupted')
            self.assertEqual(data.chats()[0]['id'], chat['id'])
            self.assertNotIn(b'synthetic request', (data.profile/'desktop-chats.sqlite3').read_bytes())

    def test_finance_matches_legacy_contract_across_missing_duplicate_and_money_cases(self):
        import copy
        import random
        from test_finance_preflight import sample
        from school_mcp.finance.preflight import PreflightRequest, preflight
        rng = random.Random(821)
        for index in range(100):
            value = copy.deepcopy(sample(rng.choice(['daily','travel','loan','remuneration','internal_transfer'])))
            if index % 2:
                value['facts'] = value['facts'][:rng.randrange(15)]
                value['claim_amount'] = rng.choice([None, '0', '0.01','0.30','999999999999.99'])
                value['contract_required'] = rng.choice([None,False,True])
                value['lines'][0].update(confirmed=bool(index%3), currency=rng.choice(['CNY','USD']),
                    amount=rng.choice([None,'0.10','0.3','1','999999999999.99']))
                if index % 3 == 0:
                    value['documents'].append(copy.deepcopy(value['documents'][0]))
                    value['lines'].append(copy.deepcopy(value['lines'][0]))
                if index % 5 == 0:
                    value['lines'][0]['document_ids']=['missing']
                if index % 7 == 0:
                    value['business_type']='auto'
                    value['lines']=[]
            request = PreflightRequest.model_validate(value)
            with patch.object(native_core, 'enabled', return_value=False):
                expected = preflight(request)
            with self.subTest(case=index):
                self.assertEqual(preflight(request), expected)

    def test_rust_finance_rejects_invalid_requests_without_python_validation(self):
        for value in [{'claim_amount':1.5}, {'claim_amount':'1e2'}, {'submit':True},
                      {'facts':[{'field':'purpose','confirmed':'yes'}]}, {'claim_amount':'0.001'}]:
            with self.assertRaises(model.ModelError) as error:
                native_core.call('finance_preflight',value)
            self.assertEqual(error.exception.code,'invalid_arguments')


if __name__ == '__main__':
    unittest.main()
