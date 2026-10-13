"""Real private-pipe/DPAPI acceptance for synthetic site sessions only."""
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import concurrent.futures
import tempfile
import time
import unittest
from unittest.mock import patch

from school_mcp.desktop import native_core
from school_mcp.desktop.model import ModelError
from school_mcp.mail.credentials import _dpapi
from school_mcp.bb import identity
from school_mcp.bb.session import BBError

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get('SCHOOL_TEST_CORE', ROOT / 'native/core/target/debug/school-core.exe'))


def cookie(name='session', domain='id.ustc.edu.cn', expires=-1, path='/'):
    return dict(name=name, value='synthetic-secret', domain=domain, path=path,
                expires=expires, secure=True, httpOnly=True, sameSite='Lax')


@unittest.skipUnless(os.name == 'nt' and CORE.is_file(), 'Windows built Rust core required')
class RustSessionTests(unittest.TestCase):
    def setUp(self):
        native_core.shutdown()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        env = patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': str(CORE), 'SCHOOL_MCP_LOCAL_DIR': directory.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(native_core.shutdown)

    def call(self, operation, profile, **values):
        return native_core.call(operation, {'profile': profile, **values})

    def path(self, name):
        return str(self.directory / name)

    def direct(self, operation, **values):
        raw = json.dumps({'operation': operation, 'input': values}).encode()
        process = subprocess.Popen([str(CORE), '--stdio'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            output, _ = process.communicate(struct.pack('>I', len(raw)) + raw, timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 0)
        size = struct.unpack('>I', output[:4])[0]
        self.assertEqual(len(output), size+4)
        return json.loads(output[4:])

    def test_existing_dpapi_can_be_loaded_then_native_save_read_by_python(self):
        path = self.directory / 'ustc-identity.device.dpapi'
        state = {'cookies': [cookie('trust')], 'origins': []}
        original = dict(account='synthetic-user', state=state, saved_at='synthetic-date', channel='chrome', context_options={})
        path.write_bytes(_dpapi(json.dumps(original).encode()))
        with patch('school_mcp.bb.identity._dpapi', side_effect=AssertionError('Python DPAPI must not execute')):
            self.assertEqual(identity.load_device_state('synthetic-user'), original)
            identity.save_device_state('synthetic-user', {'cookies': [cookie('bb', 'www.bb.ustc.edu.cn')], 'origins': []}, 'chrome', {})
        decoded = json.loads(_dpapi(path.read_bytes(), decrypt=True))
        self.assertEqual({c['name'] for c in decoded['state']['cookies']}, {'trust', 'bb'})
        self.assertNotIn(b'synthetic-secret', path.read_bytes())

    def test_cookie_scope_expiry_and_path_preserved(self):
        cookies = [cookie('own', 'www.bb.ustc.edu.cn', path='/webapps'),
                   cookie('broad', 'cn'), cookie('sibling', 'id.ustc.edu.cn'),
                   cookie('suffix', 'www.bb.ustc.edu.cn.evil.test'),
                   cookie('expired', 'www.bb.ustc.edu.cn', time.time()-10),
                   cookie('invalid-path', 'www.bb.ustc.edu.cn', path='relative')]
        result = self.call('sessions_filter', 'bb', state={'cookies': cookies})
        self.assertEqual(result['cookies'], [cookies[0]])
        self.assertTrue(result['cookies'][0]['secure'])

    def test_cookie_deletion_and_other_path_do_not_resurrect_trust(self):
        previous = {'cookies': [cookie('trust'), cookie('trust', path='/cas')], 'origins': []}
        fresh = {'cookies': [cookie('trust', expires=0)], 'origins': []}
        result = self.call('sessions_merge', 'device', previous=previous, state=fresh)
        self.assertEqual(result['cookies'], [previous['cookies'][1]])

    def test_malformed_cookie_shape_cannot_reach_browser(self):
        cookies = [cookie() | {'name': ''}, cookie() | {'value': None}, cookie() | {'sameSite': 'unsafe'},
                   {'domain': 'id.ustc.edu.cn'}, cookie()]
        result = self.call('sessions_filter', 'device', state={'cookies': cookies})
        self.assertEqual(result['cookies'], [cookies[-1]])

    def test_https_origins_and_independent_sites_cannot_take_school_state(self):
        origins = [{'origin': u} for u in ['https://id.ustc.edu.cn', 'http://id.ustc.edu.cn',
                   'https://user:secret@id.ustc.edu.cn', 'https://id.ustc.edu.cn:444',
                   'https://id.ustc.edu.cn/cas', 'https://id.ustc.edu.cn?token=secret',
                   'https://pan.ustc.edu.cn', 'https://icourse.club']]
        result = self.call('sessions_filter', 'drive', state={'cookies': [cookie(), cookie('icourse', 'icourse.club')], 'origins': origins})
        self.assertEqual([o['origin'] for o in result['origins']], ['https://id.ustc.edu.cn', 'https://pan.ustc.edu.cn'])
        result = self.call('sessions_filter', 'icourse', state={'cookies': [cookie(), cookie('icourse', 'icourse.club')], 'origins': origins})
        self.assertEqual([c['name'] for c in result['cookies']], ['icourse'])
        self.assertEqual([o['origin'] for o in result['origins']], ['https://icourse.club'])

    def test_wrong_account_device_cannot_be_loaded_or_merged(self):
        state = {'cookies': [cookie('old-trust')], 'origins': []}
        identity.save_device_state('synthetic-old', state, 'chrome', {})
        with self.assertRaises(BBError) as caught:
            identity.load_device_state('synthetic-new')
        self.assertEqual(caught.exception.code, 'identity_mismatch')
        saved = identity.save_device_state('synthetic-new', {'cookies': [cookie('new-trust')], 'origins': []}, 'chrome', {})
        self.assertEqual([c['name'] for c in saved['state']['cookies']], ['new-trust'])

    def test_new_school_identity_does_not_silently_replace_current_account(self):
        identity.save_session_identity('synthetic-first')
        before = (self.directory / 'ustc-identity.credentials.dpapi').read_bytes()
        with self.assertRaises(BBError) as caught:
            identity.save_session_identity('synthetic-other')
        self.assertEqual(caught.exception.code, 'identity_mismatch')
        self.assertEqual((self.directory / 'ustc-identity.credentials.dpapi').read_bytes(), before)
        self.assertEqual(identity.load_credentials()['password'], '')

    def test_unknown_account_never_inherits_previous_device_trust(self):
        identity.save_device_state(None, {'cookies': [cookie('unknown-old-trust')], 'origins': []}, 'chrome', {})
        saved = identity.save_device_state(None, {'cookies': [cookie('unknown-new-trust')], 'origins': []}, 'chrome', {})
        self.assertEqual([c['name'] for c in saved['state']['cookies']], ['unknown-new-trust'])
        self.assertEqual([c['name'] for c in identity.load_device_state()['state']['cookies']], ['unknown-new-trust'])

    def test_malformed_encrypted_identity_is_not_silently_overwritten(self):
        path = self.directory / 'ustc-identity.credentials.dpapi'
        path.write_bytes(_dpapi(json.dumps({'username': 'synthetic-user', 'password': None}).encode()))
        before = path.read_bytes()
        with self.assertRaises(BBError):
            identity.save_session_identity('synthetic-user')
        self.assertEqual(path.read_bytes(), before)

    def test_all_site_session_storage_uses_rust(self):
        from school_mcp.bb import session as bb
        from school_mcp.jw import session as jw
        from school_mcp.library import session as library
        from school_mcp.finance import session as finance
        from school_mcp.young import session as young
        from school_mcp.icourse import session as icourse
        from school_mcp.nan7 import session as nan7
        identity.save_session_identity('synthetic-user')
        cases = [(bb, ([cookie(domain='www.bb.ustc.edu.cn')],)),
                 (jw, ([cookie(domain='jw.ustc.edu.cn')], 'Synthetic/1')),
                 (library, ([cookie(domain='opac.lib.ustc.edu.cn')], 'https://opac.lib.ustc.edu.cn', 'Synthetic/1', 'synthetic-user')),
                 (finance, ({'cookies': [cookie(domain='cwzh.ustc.edu.cn')], 'origins': []}, 'synthetic-user', 'Synthetic/1')),
                 (young, ({'cookies': [cookie(domain='young.ustc.edu.cn')], 'origins': []}, 'synthetic-user', 'Synthetic/1')),
                 (icourse, ([cookie(domain='icourse.club')], '123', 'Synthetic/1')),
                 (nan7, ('synthetic-market-token', 'synthetic-user'))]
        for module, args in cases:
            with self.subTest(module=module.__name__), patch.object(module, '_dpapi', side_effect=AssertionError('Python DPAPI ran'), create=True), patch('school_mcp.bb.identity._dpapi', side_effect=AssertionError('Python DPAPI ran')):
                module.save_session(*args)
                self.assertIsInstance(module.load_session(), dict)
        identity.save_credentials('synthetic-switched', 'synthetic-password')
        for module, _ in cases:
            if module is icourse:
                self.assertEqual(module.load_session()['user_id'], '123')
            else:
                with self.subTest(switched=module.__name__), self.assertRaises(Exception) as caught:
                    module.load_session()
                self.assertEqual(caught.exception.code, 'identity_mismatch')

    def test_legacy_bb_jw_records_remain_readable(self):
        from school_mcp.bb import session as bb
        from school_mcp.jw import session as jw
        for module, filename in [(bb, 'bb.session.dpapi'), (jw, 'jw.session.dpapi')]:
            value = dict(base_url=module.BASE_URL, cookies=[cookie(domain=module.BASE_URL.removeprefix('https://'))], saved_at='synthetic-date', user_agent='Synthetic/1')
            (self.directory / filename).write_bytes(_dpapi(json.dumps(value).encode()))
            self.assertEqual(module.load_session(), value)

    def test_failed_validation_or_atomic_replace_preserves_previous_record(self):
        path = self.directory / 'young-session.dpapi'
        value = dict(account='synthetic-user', state={'cookies': [], 'origins': []}, saved_at='synthetic-date')
        self.call('sessions_write', 'young', path=str(path), value=value)
        before = path.read_bytes()
        with self.assertRaises(ModelError):
            self.call('sessions_write', 'young', path=str(path), value={'state': 'malformed'})
        self.assertEqual(path.read_bytes(), before)
        path.chmod(stat.S_IREAD)
        try:
            with self.assertRaises(ModelError):
                self.call('sessions_write', 'young', path=str(path), value=value | {'saved_at': 'changed'})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(self.directory.glob('.*.tmp')), [])
        finally:
            path.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_nan7_projection_excludes_cookie_and_school_credentials(self):
        value = dict(token='synthetic-token', account='synthetic-user', saved_at='synthetic-date', cookies=[cookie()], password='synthetic-secret')
        result = self.call('sessions_write', 'nan7', path=self.path('nan7-session.dpapi'), value=value)
        self.assertEqual(set(result), {'token', 'account', 'saved_at'})
        with self.assertRaises(ModelError):
            self.call('sessions_write', 'nan7', path=self.path('nan7-session.dpapi'), value=value | {'token': 'synthetic\r\nInjected: header'})

    def test_campus_shared_new_trust_replaces_old_but_site_data_stays_local(self):
        from school_mcp.campus.session import initial_state
        from school_mcp.desktop.storage import write_private
        identity.save_device_state('synthetic-user', {'cookies': [cookie('trust') | {'value': 'synthetic-new'}], 'origins': []}, 'chrome', {})
        write_private(self.directory / 'drive-campus-session.dpapi', {'account': 'synthetic-user', 'state': {'cookies': [cookie('trust') | {'value': 'synthetic-old'}, cookie('site', 'pan.ustc.edu.cn'), cookie('sibling', 'vlab.ustc.edu.cn')], 'origins': []}})
        state = initial_state('drive', 'synthetic-user')
        self.assertEqual({c['name'] for c in state['cookies']}, {'trust', 'site'})
        self.assertEqual(next(c['value'] for c in state['cookies'] if c['name']=='trust'), 'synthetic-new')
        with self.assertRaises(Exception) as caught:
            initial_state('drive', 'synthetic-other')
        self.assertEqual(caught.exception.code, 'identity_mismatch')

    def test_separate_rust_processes_merge_without_losing_device_cookies(self):
        path = self.path('ustc-identity.device.dpapi')
        identity.save_device_state('synthetic-user', {'cookies': [cookie('initial')], 'origins': []}, 'chrome', {})
        def merge_one(index):
            return self.direct('sessions_device_save', profile='device', path=path,
                               account='synthetic-user', state={'cookies': [cookie(f'trust-{index}')], 'origins': []},
                               channel='chrome', context_options={})
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(merge_one, range(12)))
        self.assertTrue(all(result.get('ok') for result in results))
        names = {c['name'] for c in identity.load_device_state('synthetic-user')['state']['cookies']}
        self.assertEqual(names, {'initial'} | {f'trust-{i}' for i in range(12)})

    def test_lock_timeout_prevents_generic_and_merge_writes(self):
        import msvcrt
        path = self.directory / 'ustc-identity.device.dpapi'
        value = identity.save_device_state('synthetic-user', {'cookies': [cookie('initial')], 'origins': []}, 'chrome', {})
        before = path.read_bytes()
        with open(str(path)+'.lock', 'a+b') as lock:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                for operation, extra in [('sessions_write', {'value': value}),
                                         ('sessions_device_save', {'state': {'cookies': [cookie('blocked')], 'origins': []}, 'channel': 'chrome', 'context_options': {}})]:
                    result = self.direct(operation, profile='device', path=str(path), account='synthetic-user', **extra)
                    self.assertFalse(result['ok'])
                    self.assertEqual(result['error']['code'], 'local_policy_unavailable')
                    self.assertEqual(path.read_bytes(), before)
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


if __name__ == '__main__':
    unittest.main()
