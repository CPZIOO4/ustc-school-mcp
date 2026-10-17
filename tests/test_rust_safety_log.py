"""Isolated native diagnostics migration contracts; no account or network access."""
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from school_mcp import safety_log as logs
from school_mcp.desktop import native_core


SECRET = 'synthetic-diagnostic-canary-never-persist'
REPO = Path(__file__).resolve().parents[1]
CORE = os.environ.get('SCHOOL_TEST_CORE', '')


class NativeDiagnosticContracts(unittest.TestCase):
    def setUp(self):
        # Prime the public tool allowlist without mocking unrelated core catalog calls.
        with patch.object(native_core, 'enabled', return_value=False):
            logs._tool_names()

    def test_compiled_source_allowlist_matches_public_python_sources(self):
        source = (REPO / 'native/core/src/safety_log_files.rs').read_text(encoding='utf-8')
        allowed = set(re.findall(r'"([a-z0-9_/]+\.py)"', source))
        expected = {p.relative_to(REPO / 'src/school_mcp').as_posix()
                    for p in (REPO / 'src/school_mcp').rglob('*.py')}
        self.assertEqual(allowed, expected)

    def test_native_record_sends_only_fixed_sanitized_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(native_core, 'enabled', return_value=True), \
                 patch.object(native_core, 'call', return_value={'error_id': 'ERR-0123456789ABCDEF', 'diagnostic_saved': True}) as call, \
                 patch.object(logs, '_db', side_effect=AssertionError('Python persistence called')):
                logs.record(root=directory, code=SECRET, service=SECRET,
                            exc=type(SECRET, (Exception,), {})(SECRET))
            operation, data = call.call_args.args
            self.assertEqual(operation, 'safety_log_record')
            self.assertEqual(data['directory'], directory)
            self.assertNotIn(SECRET, json.dumps(data))
            self.assertEqual(data['event']['code'], 'unavailable')

    def test_native_failure_does_not_fallback_or_change_business_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(native_core, 'enabled', return_value=True), \
                 patch.object(native_core, 'call', side_effect=OSError(SECRET)), \
                 patch.object(logs, '_db', side_effect=AssertionError('Python persistence called')):
                result = logs.attach({'error_code': 'timeout', 'state': 'needs_verification',
                                      'recovery': {'action': 'verify_original_receipt', 'retry_write_allowed': False}}, root=directory)
                self.assertFalse(result['diagnostic_saved'])
                self.assertFalse(result['recovery']['retry_write_allowed'])
                self.assertEqual(result['state'], 'needs_verification')
                self.assertRegex(result['error_id'], r'^ERR-[0-9A-F]{16}$')
                self.assertFalse(logs.report(directory)['available'])
                self.assertFalse(logs.summary(directory)['available'])
                with self.assertRaises(ValueError):
                    logs.clear(directory)
                with self.assertRaises(ValueError):
                    logs.export(directory)
                self.assertNotIn(SECRET, json.dumps(result))

    def test_native_default_directory_does_not_resolve_away_redirects(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'possible-junction'
            with patch.dict(os.environ, SCHOOL_MCP_LOCAL_DIR=str(original), SCHOOL_MCP_DIAGNOSTICS_DIR=''), \
                 patch.object(Path, 'resolve', side_effect=AssertionError('must retain original path')), \
                 patch.object(native_core, 'call', return_value={}) as call:
                logs._native('report')
            self.assertEqual(call.call_args.args[1]['directory'], str(original / 'diagnostics'))

    def test_desktop_and_chat_logs_no_longer_call_legacy_adapter(self):
        source = (REPO / 'native/core/src/desktop.rs').read_text(encoding='utf-8')
        incident = source.split('fn incident(', 1)[1].split('fn start(', 1)[0]
        self.assertIn('safety_log::record', incident)
        self.assertNotIn('.legacy', incident)
        for name in ('diagnostics_report', 'diagnostics_export', 'diagnostics_clear'):
            self.assertNotIn(f'self.legacy("{name}"', source)
        source = (REPO / 'native/core/src/chat_io.rs').read_text(encoding='utf-8')
        diagnostic = source.split('fn diagnostic(', 1)[1].split('fn record_failure(', 1)[0]
        self.assertIn('safety_log::attach', diagnostic)
        self.assertNotIn('.legacy', diagnostic)


@unittest.skipUnless(CORE and Path(CORE).is_file(), 'SCHOOL_TEST_CORE native binary required')
class NativeDiagnosticPipe(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'diagnostics'
        env = patch.dict(os.environ, SCHOOL_NATIVE_CORE=str(Path(CORE).resolve()),
                         SCHOOL_MCP_DIAGNOSTICS_DIR=str(self.root))
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(native_core.shutdown)
        build = patch.object(logs, '_build', return_value='a' * 64)
        build.start()
        self.addCleanup(build.stop)

    def test_cleaning_matches_python_for_supported_and_untrusted_fields(self):
        base = dict(error_id='ERR-0123456789ABCDEF', count=1, code='timeout', service='app',
                    stage='tool', outcome='unknown', first_at=1800000000, last_at=1800000001,
                    build='a' * 64, platform='win32')
        variants = [base, {**base, 'exception': SECRET, 'code': SECRET, 'tool': SECRET, 'count': True},
                    {**base, 'duration_ms': -4, 'count': 10**15, 'frames': [
                        {'file': 'desktop/chat.py', 'line': 10**15}, {'file': '../secret.py', 'line': 4}]},
                    {**base, 'tool': 'school_agent_call', 'exception': 'TimeoutError'}]
        for event in variants:
            result = native_core.call('safety_log_clean', {'event': event})
            self.assertEqual(result, logs._clean(event))

    def test_old_and_native_writers_share_format_signature_and_counts(self):
        with patch.dict(os.environ, SCHOOL_NATIVE_CORE=''):
            original = logs.record(code='timeout', outcome='read_failed')
        migrated = logs.record(code='timeout', outcome='read_failed')
        self.assertEqual(migrated['error_id'], original['error_id'])
        self.assertEqual(logs.report()['events'][0]['count'], 2)
        with patch.dict(os.environ, SCHOOL_NATIVE_CORE=''):
            third = logs.record(code='timeout', outcome='read_failed')
            self.assertEqual(third['error_id'], original['error_id'])
            self.assertEqual(logs.report()['events'][0]['count'], 3)
        self.assertEqual(logs.summary()['occurrence_count'], 3)

    def test_native_export_resanitizes_tampering_and_clear_preserves_other_files(self):
        logs.record(code='timeout')
        keep = self.root / 'keep-receipt.json'
        keep.write_text(SECRET, encoding='utf-8')
        with sqlite3.connect(self.root / 'events.sqlite3') as db:
            value = json.loads(db.execute('SELECT payload FROM events').fetchone()[0])
            value.update(code=SECRET, tool=SECRET, message=SECRET, frames=[{'file': SECRET, 'line': 5}])
            db.execute('UPDATE events SET payload=?', (json.dumps(value),))
        db.close()
        self.assertNotIn(SECRET.encode(), logs.export())
        logs.clear()
        self.assertEqual(logs.report()['events'], [])
        self.assertEqual(keep.read_text(encoding='utf-8'), SECRET)

    def test_native_writer_respects_python_byte_lock(self):
        with logs._storage_lock(self.root):
            self.assertFalse(logs.record(code='timeout')['diagnostic_saved'])
        self.assertTrue(logs.record(code='timeout')['diagnostic_saved'])
