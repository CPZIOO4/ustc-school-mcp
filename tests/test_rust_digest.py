"""Native digest contracts using synthetic local data only; no source requests."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from school_mcp.desktop import native_core
from school_mcp.desktop.model import ModelError
from school_mcp.digest import engine, native, public, sources
from school_mcp.digest.store import Store, defaults


class DigestBridgeTests(unittest.TestCase):
    def test_explicit_profile_reaches_native_without_resolving_links(self):
        with tempfile.TemporaryDirectory() as directory:
            raw=Path(directory)/'unresolved-profile'
            with patch.dict(os.environ,{'SCHOOL_MCP_LOCAL_DIR':str(raw)}), patch.object(native,'enabled',return_value=True), patch.object(native,'call',return_value={'valid':True}) as call, patch.object(Path,'resolve',side_effect=AssertionError('must not resolve before checking')):
                store=Store()
                self.assertEqual(store.root,raw)
                call.assert_called_once_with('check_paths',root=str(raw.absolute()))
            self.assertFalse(raw.exists())

    def test_native_failure_does_not_fall_back_to_python_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            with patch.object(native, 'enabled', return_value=True), patch.object(native_core, 'call', side_effect=ModelError('storage_failure', 'synthetic failure')):
                with self.assertRaises(ValueError):
                    store.read()
                self.assertFalse(store.path.exists())

    def test_native_refresh_uses_one_source_reducer_and_app_delivery(self):
        state = dict(owner='synthetic-owner', settings=defaults(), items={}, sources={}, messages=[], pending={})
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            stamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
            calls = []
            def invoke(action, **values):
                calls.append(action)
                if action == 'apply_source':
                    return {'state': values['state'], 'baseline': True}
                if action == 'finish_cycle':
                    return {'state': values['state'], 'message_id': None}
                raise AssertionError(action)
            with patch.object(store, 'read', return_value=state), patch.object(store, 'save') as save, patch.object(native, 'call', side_effect=invoke):
                result = engine._refresh_native(notify=False, store=store, collector=lambda *_: {'items': [], 'complete': True}, now=stamp, sender=None)
            self.assertEqual(calls, ['apply_source', 'finish_cycle'])
            self.assertEqual(result['baseline_sources'], ['teach'])
            save.assert_called_once()

    def test_app_only_switch_does_not_relabel_old_smtp_attempt(self):
        state = dict(owner='synthetic-owner', settings=defaults() | {'enabled': True}, items={}, sources={}, messages=[{'id': 'old', 'delivery': {'status': 'sending', 'draft_id': 'original'}}], pending={})
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            def invoke(action, **values):
                if action == 'apply_source': return {'state': values['state'], 'baseline': True}
                if action == 'finish_cycle': return {'state': values['state'], 'message_id': None}
                raise AssertionError(action)
            with patch.object(store, 'read', return_value=state), patch.object(store, 'save'), patch.object(native, 'call', side_effect=invoke), patch.object(engine, '_deliver') as deliver:
                engine._refresh_native(notify=True, store=store, collector=lambda *_: {'items': [], 'complete': True}, now=datetime(2026, 10, 1, tzinfo=timezone.utc), sender=None)
            deliver.assert_not_called()
            self.assertEqual(state['messages'][0]['delivery'], {'status': 'sending', 'draft_id': 'original'})


@unittest.skipUnless(os.name == 'nt' and os.environ.get('SCHOOL_TEST_CORE'), 'requires explicit built native core on Windows')
class RustDigestContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': os.environ['SCHOOL_TEST_CORE'], 'SCHOOL_MCP_LOCAL_DIR': str(self.root)})
        self.env.start()
        self.owner = patch('school_mcp.digest.store.owner', return_value='synthetic-owner')
        self.owner.start()
        self.store = Store(self.root)

    def tearDown(self):
        native_core.shutdown()
        self.owner.stop()
        self.env.stop()
        self.tmp.cleanup()

    def call(self, action, **values):
        return native.call(action, **values)

    def legacy(self, function, *args, **kwargs):
        with patch.object(native, 'enabled', return_value=False):
            return function(*args, **kwargs)

    def row(self, identifier='one', **values):
        return public.item('teach', identifier, '合成 AI 讲座', 'https://www.teach.ustc.edu.cn/synthetic', summary='明确的合成信息', published_at='2026-10-01', **values)

    def test_settings_catalog_and_fingerprints_match_legacy(self):
        self.assertEqual(self.call('defaults'), defaults())
        self.assertEqual(self.call('catalog'), sources.catalog())
        for value in [{}, {'sources': ['teach', 'teach'], 'keywords': [' AI ', 'AI']}, {'email': 'example+digest@example.com'}, {'sources': ['bb'], 'bb_courses': ['_123_1']}]:
            self.assertEqual(engine.settings(value), self.legacy(engine.settings, value))
        for value in [{'email': '.x@example.com'}, {'email': 'x@example.c'}, {'daily_time': '24:00'}, {'interval_minutes': True}, {'sources': ['bb']}]:
            with self.assertRaises(ValueError): engine.settings(value)
        row = self.row(poster_ocr={'images': [{'hash': 'abc'}], 'errors': ['transient']})
        self.assertEqual(engine.fingerprint(row), self.legacy(engine.fingerprint, row))
        for confidence in [1e-7, 1e-5, 1e16, -0.0, .98]:
            row['poster_ocr']['minimum_confidence'] = confidence
            self.assertEqual(engine.fingerprint(row), self.legacy(engine.fingerprint, row))
        self.assertEqual(engine.report_text([row], {'teach': {'state': 'ok', 'complete': False}}), self.legacy(engine.report_text, [row], {'teach': {'state': 'ok', 'complete': False}}))

    def test_legacy_dpapi_bidirectional_and_owner_protection(self):
        old = self.legacy(self.store.read)
        old['messages'] = [dict(id='old', text='PRIVATE-CANARY', read=False, delivery={'status': 'unknown', 'draft_id': 'preserve'})]
        self.legacy(self.store.save, old)
        self.assertEqual(self.store.read(), old)
        engine.mark_read('old', self.store)
        updated = self.legacy(self.store.read)
        self.assertTrue(updated['messages'][0]['read'])
        self.assertEqual(updated['messages'][0]['delivery'], old['messages'][0]['delivery'])
        self.assertNotIn(b'PRIVATE-CANARY', self.store.path.read_bytes())
        with patch('school_mcp.digest.store.owner', return_value='other-owner'):
            with self.assertRaises(ValueError): self.store.read()
            with self.assertRaises(ValueError): self.store.save(old)
        self.assertEqual(self.store.read(), updated)

    def test_cross_process_cycle_lock_and_preview_dedup(self):
        with self.store.cycle():
            with self.assertRaises(ValueError): engine.preview_report(self.store)
        first = engine.preview_report(self.store)
        second = engine.preview_report(self.store)
        self.assertEqual(first['message_id'], second['message_id'])
        self.assertEqual(second['state'], 'existing')
        self.assertEqual(first['delivery'], {'status': 'app_only'})
        self.assertFalse(self.store.read()['settings']['email_enabled'])

    def test_baseline_change_daily_preview_and_unknown_preserved(self):
        now = datetime(2026, 10, 1, 2, tzinfo=timezone.utc)
        engine.configure(defaults() | dict(enabled=True, urgent_hours=0, daily_time='10:00'), True, self.store)
        def cycle(rows, day, notify=True):
            return engine.refresh(store=self.store, collector=lambda *_: {'items': copy.deepcopy(rows), 'complete': True}, now=now + timedelta(days=day), notify=notify)
        cycle([self.row()], 0)
        self.assertFalse(self.store.read()['messages'])
        cycle([self.row(), self.row('two')], 1, False)
        self.assertEqual(len(self.store.read()['pending']), 1)
        snapshot = engine.preview_report(self.store)
        self.assertEqual(snapshot['delivery']['status'], 'app_only')
        self.assertEqual(len(self.store.read()['pending']), 1)
        cycle([self.row(), self.row('two')], 2)
        state = self.store.read()
        self.assertEqual(len(state['messages']), 2)
        self.assertEqual(state['messages'][-1]['delivery']['status'], 'app_only')
        cycle([self.row(), self.row('two')], 3)
        self.assertEqual(len(self.store.read()['messages']), 2)

    def test_invalid_collector_preserves_prior_snapshot(self):
        stamp = datetime(2026, 10, 1, 2, tzinfo=timezone.utc)
        engine.refresh(store=self.store, collector=lambda *_: {'items': [self.row()], 'complete': True}, now=stamp)
        old = self.store.read()['items']
        engine.refresh(store=self.store, collector=lambda *_: {'items': [{'id': 'wrong', 'source': 'portal'}]}, now=stamp + timedelta(days=1))
        state = self.store.read()
        self.assertEqual(state['items'], old)
        self.assertEqual(state['sources']['teach']['state'], 'failed')

    def test_symlink_profile_and_dangling_sqlite_sidecar_rejected(self):
        outside=self.root/'outside';outside.mkdir()
        link=self.root/'profile-link'
        try:link.symlink_to(outside,target_is_directory=True)
        except OSError as exc:self.skipTest('Windows symlink permission unavailable: '+str(exc.winerror))
        with self.assertRaises(ValueError):Store(link/'new-profile')
        self.assertFalse((outside/'new-profile').exists())
        link.unlink()
        sidecar=self.root/'digest.sqlite3-wal'
        sidecar.symlink_to(outside/'absent')
        with self.assertRaises(ValueError):self.store.read()
        self.assertFalse((outside/'absent').exists())
        sidecar.unlink()


if __name__ == '__main__':
    unittest.main()
