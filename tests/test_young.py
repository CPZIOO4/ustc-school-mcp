from __future__ import annotations

import json
import asyncio
import threading
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from school_mcp.bb.identity import save_credentials
from school_mcp.young.client import YoungClient
from school_mcp.young.session import YoungError, load_session, own_state, save_session


class YoungBoundaryTests(unittest.TestCase):
    def test_own_state_keeps_only_exact_https_origin_and_site_cookies(self):
        state = own_state({"cookies": [{"domain":"young.ustc.edu.cn","value":"synthetic-token"},
                                      {"domain":"id.ustc.edu.cn"},{"domain":".ustc.edu.cn"},{"domain":"evil.example"}],
                           "origins": [{"origin":"https://young.ustc.edu.cn"},{"origin":"http://young.ustc.edu.cn"},
                                       {"origin":"https://young.ustc.edu.cn.evil.example"}]})
        self.assertEqual(len(state['cookies']),1)
        self.assertEqual(len(state['origins']),1)

    def test_invalid_limits_do_not_load_credentials_or_start_browser(self):
        with patch('school_mcp.young.client.load_session') as load:
            for limit in [0,50001,True,'1']:
                with self.assertRaises(YoungError):
                    YoungClient().home(limit)
            load.assert_not_called()

    @unittest.skipUnless(os.name=='nt','Windows DPAPI')
    def test_encrypted_state_and_account_binding(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'SCHOOL_MCP_LOCAL_DIR':directory}):
            save_credentials('synthetic-user','synthetic-password')
            state={'cookies':[],'origins':[{'origin':'https://young.ustc.edu.cn','localStorage':[{'name':'token','value':'synthetic-token'}]}]}
            save_session(state,'synthetic-user','SyntheticBrowser/1')
            self.assertNotIn(b'synthetic-token',(Path(directory)/'young-session.dpapi').read_bytes())
            self.assertEqual(load_session()['state'],state)
            from school_mcp.young.reconnect import status
            self.assertNotIn('synthetic-token',json.dumps(status()))
            save_credentials('another-user','synthetic-password')
            with self.assertRaisesRegex(YoungError,'不一致'):
                load_session()

    def test_background_challenge_is_reported_before_stop(self):
        from school_mcp.browser import BackgroundVerificationRequired
        from school_mcp.young.login import login_state
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'SCHOOL_MCP_LOCAL_DIR':directory,'SCHOOL_MCP_BROWSER_HEADED':'0'}):
            with self.assertRaises(BackgroundVerificationRequired):
                login_state('waiting_for_verification','synthetic challenge')
            data=json.loads((Path(directory)/'young.login-status.json').read_text(encoding='utf-8'))
            self.assertTrue(data['requires_user_action'])


class YoungMCPThreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_tools_run_outside_mcp_event_loop(self):
        from school_mcp.young.server import school_young_check_connection, school_young_read_home
        main_thread = threading.get_ident()
        def check():
            self.assertNotEqual(threading.get_ident(), main_thread)
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return {'connected':True}
        def home(max_chars):
            self.assertEqual(max_chars,123)
            return check()
        with patch('school_mcp.young.server.YoungClient') as client:
            client.return_value.check.side_effect=check
            client.return_value.home.side_effect=home
            self.assertTrue((await school_young_check_connection())['connected'])
            self.assertTrue((await school_young_read_home(123))['connected'])


if __name__=='__main__':
    unittest.main()
