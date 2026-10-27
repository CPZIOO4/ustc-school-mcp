import hashlib, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch, Mock
from school_mcp.desktop.storage import read_private, write_private
from school_mcp.desktop import identity_bridge as bridge
from school_mcp.bb.identity import save_session_identity
from school_mcp.service_errors import ServiceError
from school_mcp.login_guidance import reason

class IdentityBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'app';self.source=Path(self.temp.name)/'mcp'
        self.account='synthetic-owner';self.key=hashlib.sha256(self.account.encode()).hexdigest()
        write_private(self.root/'profile/ustc-identity.credentials.dpapi',{'username':self.account,'password':'','session_only':True})
        write_private(self.source/'ustc-identity.credentials.dpapi',{'username':self.account,'password':'synthetic-secret','email_verification_enabled':True})
        write_private(self.root/'shared-identity-link.dpapi',{'source':str(self.source),'owner':self.key,'allow_recovery':True})
        self.stop=Mock();self.stop.is_set.return_value=False
    def test_recovery_scoped_and_copies_only_sessions(self):
        write_private(self.source/'bb.session.dpapi',{'account':self.account,'cookies':[]})
        def connect(service,*args,**kwargs):
            self.assertEqual(service,'bb');self.assertEqual(os.environ['SCHOOL_MCP_LOCAL_DIR'],str(self.source));return {'connected':True}
        calls=[]
        def native(action,value):
            calls.append((action,value))
            if action=='sessions_read':return {'account':self.account,'cookies':[]}
            return {'saved':True}
        with patch.dict(os.environ,{'SCHOOL_MCP_LOCAL_DIR':'original'}),patch('school_mcp.desktop.connections.connect',side_effect=connect),patch('school_mcp.desktop.native_core.call',side_effect=native):
            value=bridge.restore(self.root,'bb',self.stop,lambda *_:None)
            self.assertEqual(os.environ['SCHOOL_MCP_LOCAL_DIR'],'original')
        self.assertTrue(value['connected']);self.assertFalse(value['credentials_copied'])
        self.assertTrue(all('credentials' not in str(x) for x in calls))
        self.assertEqual(read_private(self.root/'profile/ustc-identity.credentials.dpapi')['password'],'')
    def test_mismatch_lock_and_no_link_cannot_restore(self):
        with patch('school_mcp.desktop.connections.connect') as connect:
            (self.root/'locked').touch()
            with self.assertRaises(ServiceError):bridge.restore(self.root,'bb',self.stop,lambda *_:None)
            (self.root/'locked').unlink()
            write_private(self.source/'ustc-identity.credentials.dpapi',{'username':'other'})
            with self.assertRaises(ServiceError):bridge.restore(self.root,'bb',self.stop,lambda *_:None)
            connect.assert_not_called()
        (self.root/'shared-identity-link.dpapi').unlink()
        self.assertEqual(bridge.restore(self.root,'bb',self.stop,lambda *_:None),{'linked':False})
    def test_cooldown_is_preserved_no_session_copy(self):
        with patch('school_mcp.desktop.connections.connect',return_value={'connected':False,'error_code':'cooldown','retry_after_seconds':90}),patch('school_mcp.desktop.native_core.call') as native:
            value=bridge.restore(self.root,'bb',self.stop,lambda *_:None)
            self.assertEqual(value['retry_after_seconds'],90);native.assert_not_called()
    def test_manual_sign_in_keeps_saved_recovery_credentials(self):
        with patch.dict(os.environ,{'SCHOOL_MCP_LOCAL_DIR':str(self.source)}):save_session_identity(self.account)
        record=read_private(self.source/'ustc-identity.credentials.dpapi')
        self.assertEqual(record['password'],'synthetic-secret');self.assertTrue(record['email_verification_enabled']);self.assertFalse(record['session_only'])
    def test_guidance_is_fixed_not_echoed(self):
        self.assertEqual(reason('学校要求图形或滑块验证'),'login_captcha_required')
        self.assertEqual(reason('学校未提供可自动完成的邮箱验证，需要本人完成当前验证。'),'manual_verification_required')
        self.assertEqual(reason('untrusted secret content'),'manual_verification_required')

    def test_deleted_source_identity_has_explicit_failure(self):
        (self.source/'ustc-identity.credentials.dpapi').unlink()
        with patch('school_mcp.desktop.connections.connect') as connect:
            with self.assertRaises(ServiceError) as error:bridge.restore(self.root,'bb',self.stop,lambda *_:None)
            self.assertEqual(error.exception.code,'identity_mismatch')
            connect.assert_not_called()

    @unittest.skipUnless(os.environ.get('SCHOOL_NATIVE_CORE_TEST'), 'requires native core')
    def test_native_session_save_preserves_existing_recovery(self):
        from school_mcp.desktop import native_core
        with patch.dict(os.environ, SCHOOL_NATIVE_CORE=os.environ['SCHOOL_NATIVE_CORE_TEST']):
            path=self.source/'ustc-identity.credentials.dpapi'
            native_core.call('sessions_identity_save',{'profile':'identity','path':str(path),'username':self.account})
            saved=read_private(path)
            self.assertEqual(saved['password'],'synthetic-secret')
            self.assertTrue(saved['email_verification_enabled'])
            self.assertFalse(saved['session_only'])

if __name__=='__main__':unittest.main()
