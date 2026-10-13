"""Real Rust desktop process with the fixed Python compatibility adapter."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import socket
import time
import tempfile
import unittest
import httpx

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get('SCHOOL_TEST_CORE', ROOT / 'native/core/target/debug/school-core.exe'))


@unittest.skipUnless(os.name == 'nt' and CORE.is_file(), 'build Windows Rust core first')
class RustDesktopTests(unittest.TestCase):
    def test_old_python_instance_remains_owner(self):
        with tempfile.TemporaryDirectory(prefix='school-rust-old-instance-') as root:
            env = dict(os.environ, SCHOOL_LEGACY_ENGINE=str(ROOT / '.venv/Scripts/python.exe'))
            env.pop('SCHOOL_NATIVE_CORE', None)
            old = subprocess.Popen([sys.executable, '-X', 'utf8', '-m', 'school_mcp.desktop.server',
                                    '--data-dir', root, '--native-stdio', '--no-open'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    previous = json.loads(pool.submit(old.stdout.readline).result(30))
                new = subprocess.run([str(CORE), '--desktop', '--native-stdio', '--data-dir', root],
                                     input=b'', capture_output=True, env=env, timeout=30,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
                value = json.loads(new.stdout)
                self.assertEqual(new.returncode, 0)
                self.assertFalse(value['owned'])
                self.assertEqual(value['token'], previous['token'])
                self.assertNotEqual((value.get('core') or {}).get('desktop_backend'), 'rust')
                self.assertIsNone(old.poll())
            finally:
                old.stdin.close()
                old.wait(20)
                old.stdout.close()

    def test_actual_native_entry_private_handshake_adapter_and_eof(self):
        with tempfile.TemporaryDirectory(prefix='school-rust-desktop-') as root:
            env = dict(os.environ, SCHOOL_LEGACY_ENGINE=str(ROOT / '.venv/Scripts/python.exe'))
            cmd = [str(CORE), '--desktop', '--native-stdio', '--data-dir', root]
            process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    hello = json.loads(pool.submit(process.stdout.readline).result(30))
                self.assertEqual(hello['core']['desktop_backend'], 'rust')
                self.assertEqual(hello['core']['version'], '0.8.0')
                self.assertTrue(hello['owned'])
                second = subprocess.run(cmd, input=b'', capture_output=True, timeout=20, env=env,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                other = json.loads(second.stdout)
                self.assertFalse(other['owned'])
                self.assertEqual(hello['token'], other['token'])
                old = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'school_mcp.desktop.server',
                                      '--data-dir', root, '--native-stdio', '--no-open'],
                                     input=b'', capture_output=True, timeout=30, env=env,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(old.returncode, 0, old.stderr.decode(errors='replace'))
                old_hello = json.loads(old.stdout)
                self.assertFalse(old_hello['owned'])
                self.assertEqual(old_hello['token'], hello['token'])
                headers = {'X-School-Token': hello['token'], 'Origin': hello['origin']}
                # Accepted Windows sockets inherit listener flags; a client is
                # allowed to connect before sending its request headers.
                with socket.create_connection(('127.0.0.1', int(hello['origin'].rsplit(':', 1)[1]))) as delayed:
                    time.sleep(.15)
                    delayed.sendall((f"GET / HTTP/1.1\r\nHost: {hello['origin'].removeprefix('http://')}\r\n\r\n").encode())
                    self.assertTrue(delayed.recv(4096).startswith(b'HTTP/1.1 200'))
                with httpx.Client(base_url=hello['origin'], trust_env=False, timeout=20) as client:
                    self.assertEqual(client.get('/api/status').status_code, 403)
                    status = client.get('/api/status', headers=headers)
                    self.assertEqual(status.status_code, 200, status.text)
                    self.assertFalse(status.json()['identity']['saved'])
                    sites = client.get('/api/campus/sites', headers=headers).json()
                    self.assertEqual(len(sites['sites']), 17)
                    self.assertEqual(client.get('/api/chats', headers=headers).status_code, 400)
                    self.assertEqual(client.post('/api/stop', headers=headers, json={}).status_code, 200)
                    self.assertEqual(client.get('/api/status', headers=headers).status_code, 200)
                    failure = client.post('/api/diagnostics/ui-error', headers=headers, json={'message': 'synthetic-secret'})
                    self.assertEqual(failure.status_code, 200)
                    logs = client.get('/api/diagnostics/export', headers=headers)
                    self.assertEqual(logs.status_code, 200)
                    self.assertNotIn('synthetic-secret', logs.text)
                    from school_mcp.desktop.storage import write_private
                    write_private(Path(root) / 'adapter-uncertain.dpapi', {'state': 'unknown', 'action': 'synthetic'})
                    clear = client.post('/api/clear', headers=headers, json={'scope': 'all', 'confirmation': 'all'})
                    self.assertEqual(clear.status_code, 400)
                    self.assertTrue((Path(root) / 'adapter-uncertain.dpapi').exists())
                process.stdin.close()
                self.assertEqual(process.wait(20), 0)
                self.assertFalse((Path(root) / 'instance.dpapi').exists())
            finally:
                if process.poll() is None:
                    process.stdin.close()
                    process.wait(30)
                process.stdout.close()


if __name__ == '__main__':
    unittest.main()
