from __future__ import annotations

from network_test_support import isolate_pacing as setUpModule

import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from importlib import import_module
import json
from pathlib import Path

from school_mcp.browser import BackgroundVerificationRequired, chrome_browser


class BackgroundBrowserTests(unittest.TestCase):
    def test_default_chrome_headless_and_cleanup_on_failure(self):
        p = Mock()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                with chrome_browser(p):
                    raise ValueError('synthetic failure')
        p.chromium.launch.assert_called_once_with(channel='chrome', headless=True)
        p.chromium.launch.return_value.close.assert_called_once()

    def test_visible_window_requires_explicit_override(self):
        p = Mock()
        with patch.dict(os.environ, {'SCHOOL_MCP_BROWSER_HEADED': '1'}):
            with chrome_browser(p):
                pass
        p.chromium.launch.assert_called_once_with(channel='chrome', headless=False)

    def test_all_adapters_report_then_stop_instead_of_opening_window(self):
        for adapter, filename in [('bb','bb.login-status.json'),('jw','jw.login-status.json'),('library','library.login-status.json'),('nan7','nan7-login-status.json')]:
            with self.subTest(adapter=adapter), tempfile.TemporaryDirectory() as directory:
                with patch.dict(os.environ, {'SCHOOL_MCP_LOCAL_DIR':directory,'SCHOOL_MCP_BROWSER_HEADED':'0'}):
                    notify = import_module(f'school_mcp.{adapter}.login').login_state
                    with self.assertRaises(BackgroundVerificationRequired):
                        notify('waiting_for_verification','synthetic challenge')
                    data=json.loads((Path(directory)/filename).read_text(encoding='utf-8'))
                    self.assertTrue(data['headless'])
                    self.assertTrue(data['requires_user_action'])
                    self.assertEqual(data['stage'],'waiting_for_verification')


if __name__ == '__main__':
    unittest.main()
