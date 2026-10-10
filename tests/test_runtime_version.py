import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from school_mcp.runtime_version import RuntimeMonitor, snapshot


class RuntimeVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='school runtime 中文 ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = self.root / 'src/school_mcp'
        self.package.mkdir(parents=True)
        (self.package / '__init__.py').write_text('# fixture\n')
        (self.package / 'runtime_version.py').write_text('# fixture\n')
        (self.root / 'uv.lock').write_text('version = 1\n')

    def test_zip_install_without_git_and_private_changes_do_not_invalidate(self):
        monitor = RuntimeMonitor(self.package)
        initial = monitor.report()
        private = self.root / '.local'
        private.mkdir()
        (private / 'secret.py').write_text('private should never be hashed')
        (self.root / 'README.md').write_text('documentation only')
        (self.package / '__pycache__').mkdir()
        (self.package / '__pycache__/generated.py').write_text('ignored cache')
        result = monitor.report(initial['startup_fingerprint'])
        self.assertEqual(result['state'], 'current')
        self.assertTrue(result['expected_matches'])
        self.assertFalse(result['restart_required'])
        self.assertFalse(result['credentials_read'])
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_edit_add_delete_and_restored_timestamp_are_detected(self):
        target = self.package / 'feature.py'
        target.write_text('x=1\n')
        monitor = RuntimeMonitor(self.package)
        stamp = target.stat()
        target.write_text('x=2\n')
        os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        changed = monitor.report()
        self.assertEqual(changed['state'], 'restart_required')
        self.assertFalse(changed['dependencies_changed'])
        self.assertIn('reload_client', changed['actions'])
        target.unlink()
        self.assertTrue(monitor.report()['restart_required'])
        target.write_text('x=1\n')
        self.assertEqual(monitor.report()['state'], 'current')
        (self.package / 'added.py').write_text('# new module')
        self.assertTrue(monitor.report()['restart_required'])

    def test_dependency_changes_and_wrong_checkout_are_distinct(self):
        monitor = RuntimeMonitor(self.package)
        report = monitor.report('0' * 64)
        self.assertEqual(report['state'], 'expected_mismatch')
        self.assertFalse(report['restart_required'])
        self.assertFalse(report['expected_matches'])
        (self.root / 'uv.lock').write_text('version = 2\n')
        report = monitor.report()
        self.assertTrue(report['dependency_sync_required'])
        self.assertEqual(report['actions'][0], 'sync_dependencies')
        self.assertTrue(report['dependencies_changed'])

    def test_installed_dependency_metadata_change_is_detected(self):
        with patch('school_mcp.runtime_version.metadata.version', return_value='1.0'):
            monitor = RuntimeMonitor(self.package)
        with patch('school_mcp.runtime_version.metadata.version', return_value='2.0'):
            report = monitor.report()
        self.assertTrue(report['restart_required'])
        self.assertTrue(report['dependencies_changed'])
        self.assertFalse(report['dependency_sync_required'])

    def test_unreadable_missing_or_incomplete_startup_is_not_current(self):
        monitor = RuntimeMonitor(self.package)
        with patch('pathlib.Path.open', side_effect=PermissionError('private path')):
            report = monitor.report()
            broken_start = RuntimeMonitor(self.package)
        self.assertEqual(report['state'], 'check_failed')
        self.assertIsNone(report['restart_required'])
        self.assertNotIn('private path', json.dumps(report))
        self.assertEqual(broken_start.report()['state'], 'check_failed')
        (self.package / '__init__.py').unlink()
        self.assertEqual(monitor.report()['state'], 'check_failed')

    def test_oversized_and_symlink_source_fail_without_reading_external_data(self):
        target = self.package / 'large.py'
        target.write_bytes(b'x' * (2 * 1024 * 1024 + 1))
        self.assertIsNone(snapshot(self.package)['fingerprint'])
        target.unlink()
        with patch('pathlib.Path.is_symlink', return_value=True), patch('pathlib.Path.open') as read:
            self.assertIsNone(snapshot(self.package)['fingerprint'])
        read.assert_not_called()

    def test_invalid_expected_fingerprint_is_rejected(self):
        monitor = RuntimeMonitor(self.package)
        for value in ['', 'secret-value', 123, 'A'*64]:
            with self.assertRaises(ValueError):
                monitor.report(value)

    def test_real_stdio_old_process_detects_update_then_new_process_matches(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        import school_mcp.runtime_version as module
        shutil.copyfile(module.__file__, self.package / 'runtime_version.py')
        (self.package / '__init__.py').write_text('from .runtime_version import MONITOR\n')
        target = self.package / 'feature.py'
        target.write_text('x=1\n')
        expected_before = snapshot(self.package)['fingerprint']
        script = ("import sys; sys.path.insert(0,sys.argv[1]); "
                  "from school_mcp.runtime_version import install; "
                  "from mcp.server.fastmcp import FastMCP; "
                  "m=FastMCP('runtime-fixture'); install(m,'probe'); m.run(transport='stdio')")
        params = StdioServerParameters(command=sys.executable, args=['-c', script, str(self.root / 'src')],
            env={**os.environ, 'PYTHONUTF8':'1', 'PYTHONDONTWRITEBYTECODE':'1', 'SCHOOL_MCP_LOCAL_DIR':str(self.root / '.local')})

        async def scenario():
            with open(os.devnull, 'w') as err:
                async with stdio_client(params, errlog=err) as (r,w):
                    async with ClientSession(r,w) as session:
                        await session.initialize()
                        # Edit before the first status call: baseline must come from startup.
                        target.write_text('x=2\n')
                        result = await session.call_tool('school_probe_runtime_status', {})
                        old = result.structuredContent
                        self.assertEqual(old['state'], 'restart_required')
                        self.assertEqual(old['startup_fingerprint'], expected_before)
                        expected_after = old['disk_fingerprint']
                async with stdio_client(params, errlog=err) as (r,w):
                    async with ClientSession(r,w) as session:
                        await session.initialize()
                        result = await session.call_tool('school_probe_runtime_status', {'expected_fingerprint':expected_after})
                        new = result.structuredContent
                        self.assertEqual(new['state'], 'current')
                        self.assertTrue(new['expected_matches'])
                        self.assertNotEqual(old['instance_id'], new['instance_id'])
        asyncio.run(asyncio.wait_for(scenario(), timeout=35))


if __name__ == '__main__':
    unittest.main()
