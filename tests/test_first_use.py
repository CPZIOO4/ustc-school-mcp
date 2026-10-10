import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from school_mcp.first_use import first_use

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('school_bootstrap', ROOT / 'bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class FirstUseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='school setup 中文 ')
        self.addCleanup(self.tmp.cleanup)
        self.environment = patch.dict(os.environ, {'SCHOOL_MCP_LOCAL_DIR': self.tmp.name, 'SCHOOL_MAIL_PASSWORD': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_empty_profile_routes_without_network_or_decryption(self):
        with patch('school_mcp.first_use.diagnose') as network, patch('school_mcp.mail.credentials._dpapi') as decrypt:
            result = first_use(['mail', 'jw', 'teach', 'icourse', 'mail'])
        network.assert_not_called()
        decrypt.assert_not_called()
        self.assertEqual([x['state'] for x in result['checks']], ['setup_required', 'identity_required', 'public_read_ready', 'public_read_ready'])
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_presence_never_means_connected_and_account_omitted(self):
        path = Path(self.tmp.name)
        (path / 'mail.json').write_text(json.dumps({'address': 'synthetic-user@mail.ustc.edu.cn'}))
        (path / 'mail.credentials.dpapi').write_bytes(b'synthetic-broken-ciphertext')
        (path / 'jw.session.dpapi').write_bytes(b'synthetic-session')
        result = first_use(['mail', 'jw'])
        self.assertEqual([x['state'] for x in result['checks']], ['configured_unchecked', 'session_unchecked'])
        self.assertNotIn('synthetic-user', json.dumps(result))
        self.assertFalse(result['network_checked'])

    def test_network_is_explicit_and_selected(self):
        with patch('school_mcp.first_use.diagnose', return_value={'all_connected': False}) as diagnose:
            result = first_use(['teach', 'teach'], check_connections=True)
        diagnose.assert_called_once_with(['teach'])
        self.assertFalse(result['connections']['all_connected'])
        self.assertTrue(result['network_checked'])

    def test_configuration_preserves_spaces_unicode_and_only_requested_servers(self):
        executable = Path(self.tmp.name) / 'Scripts' / 'python.exe'
        executable.parent.mkdir()
        executable.touch()
        with patch.object(bootstrap, 'python_path', return_value=executable):
            result = bootstrap.configuration(['mail', 'teach', 'mail'], Path(self.tmp.name))
        self.assertEqual(set(result['mcpServers']), {'ustc-mail', 'ustc-teach'})
        for config in result['mcpServers'].values():
            self.assertEqual(config['command'], str(executable))
            self.assertEqual(config['env']['SCHOOL_MCP_LOCAL_DIR'], str(Path(self.tmp.name).resolve()))
            self.assertEqual(config['args'][:2], ['-m', 'school_mcp'])
            self.assertNotIn('SCHOOL_MAIL_PASSWORD', config['env'])
        self.assertNotIn('cwd', json.dumps(result))

    def test_config_requires_installed_environment(self):
        with patch.object(bootstrap, 'python_path', return_value=Path(self.tmp.name) / 'absent'):
            with self.assertRaises(ValueError):
                bootstrap.configuration(['mail'], Path(self.tmp.name))


if __name__ == '__main__':
    unittest.main()
