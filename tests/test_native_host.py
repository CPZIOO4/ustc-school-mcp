"""Native pipe handshake, instance ownership and parent-death cleanup."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from school_mcp.desktop.server import native_handshake
from school_mcp.desktop.server import Server
from school_mcp.desktop.app import Application


class NativeHandshake(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows business engine')
    def test_keepalive_reuses_success_and_discards_rejected_body(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ):
            with Server(Application(root)) as server:
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    with httpx.Client(base_url=server.origin,trust_env=False,
                                      headers={'X-School-Token':server.token,'Origin':server.origin}) as client:
                        first=client.get('/api/status');second=client.get('/api/status')
                        self.assertEqual(first.status_code,200)
                        self.assertIs(first.extensions['network_stream'],second.extensions['network_stream'])
                        rejected=client.post('/api/login',content=b'x'*200001)
                        self.assertEqual(rejected.status_code,413)
                        self.assertEqual(rejected.headers['Connection'],'close')
                        self.assertEqual(client.get('/api/status').status_code,200)
                finally:server.shutdown();thread.join(5)

    def test_private_pipe_schema_and_rejects_nonlocal_origins(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            native_handshake('http://127.0.0.1:3718/#' + 'a' * 43, owned=True)
        value = json.loads(out.getvalue())
        self.assertEqual(value['origin'], 'http://127.0.0.1:3718')
        self.assertTrue(value['owned'])
        for url in ['https://example.test/#' + 'a'*43, 'http://user@127.0.0.1:1/#'+'a'*43,
                    'http://127.0.0.1:1/other#'+'a'*43, 'http://127.0.0.1:1/#short']:
            with self.assertRaises(ValueError): native_handshake(url, owned=False)

    @unittest.skipUnless(os.name == 'nt', 'Windows business engine')
    def test_native_owner_reuses_instance_then_releases_on_pipe_eof(self):
        with tempfile.TemporaryDirectory(prefix='school-native-pipe-') as root:
            cmd = [sys.executable, '-X', 'utf8', '-m', 'school_mcp.desktop.server',
                   '--data-dir', root, '--native-stdio', '--no-open']
            with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  creationflags=subprocess.CREATE_NO_WINDOW) as process:
                try:
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        hello = json.loads(pool.submit(process.stdout.readline).result(30))
                    self.assertTrue(hello['owned'])
                    second = subprocess.run(cmd, input=b'', capture_output=True, timeout=20,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
                    other = json.loads(second.stdout)
                    self.assertEqual(second.returncode, 0)
                    self.assertFalse(other['owned'])
                    self.assertEqual(hello['token'], other['token'])
                    with httpx.Client(base_url=hello['origin'], trust_env=False) as client:
                        self.assertEqual(client.get('/api/status').status_code, 403)
                        status = client.get('/api/status', headers={'X-School-Token': hello['token']})
                        self.assertEqual(status.status_code, 200)
                        self.assertFalse(status.json()['identity']['saved'])
                    process.stdin.close()
                    self.assertEqual(process.wait(15), 0)
                    self.assertFalse((Path(root)/'instance.dpapi').exists())
                finally:
                    if process.poll() is None:
                        process.terminate(); process.wait(10)


if __name__ == '__main__': unittest.main()
