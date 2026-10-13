"""Fault-injection and privacy tests. Synthetic data, no school/network writes."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from mcp.server.fastmcp import FastMCP
from school_mcp import safety_log as logs
from school_mcp.recovery import failure

SECRET = 'synthetic-secret-canary-DO-NOT-EXPORT'


class LogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'diagnostics'
        env = patch.dict(os.environ, SCHOOL_MCP_DIAGNOSTICS_DIR=str(self.root))
        env.start(); self.addCleanup(env.stop)

    def test_no_private_fields_prose_paths_urls_or_dynamic_exception_names(self):
        Evil = type(SECRET, (Exception,), {})
        try:
            raise Evil('https://school.example/?ticket='+SECRET)
        except Exception as exc:
            v = logs.record(exc=exc, code=SECRET, service=SECRET, stage=SECRET, tool=SECRET)
        self.assertTrue(v['diagnostic_saved'])
        exported = logs.export()
        self.assertNotIn(SECRET.encode(), exported)
        self.assertNotIn(SECRET.encode(), (self.root / 'events.sqlite3').read_bytes())
        self.assertNotIn(str(self.tmp.name).encode(), exported)
        e = json.loads(exported)['events'][0]
        self.assertEqual(e['exception'], 'Exception')
        self.assertEqual(e['code'], 'unavailable')
        self.assertEqual(e['frames'], [])

    def test_repeat_grouping_expiry_and_bounded_database(self):
        initial = int(time.time())
        with patch.object(logs.time, 'time', return_value=initial):
            a=logs.record(code='timeout', outcome='read_failed')
            b=logs.record(code='timeout', outcome='read_failed')
        self.assertEqual(a['error_id'], b['error_id'])
        self.assertEqual(logs.report()['events'][0]['count'], 2)
        with patch.object(logs, 'MAX_EVENTS', 3):
            for i in range(5):
                with patch.object(logs.time, 'time', return_value=initial+(i+1)*301):
                    logs.record(code='timeout')
            self.assertEqual(len(logs.report()['events']), 3)
        with patch.object(logs.time, 'time', return_value=initial+15*86400):
            self.assertEqual(logs.report()['events'], [])
        self.assertLess((self.root/'events.sqlite3').stat().st_size, logs.MAX_DATABASE_BYTES)

    def test_export_resanitizes_manually_injected_fields(self):
        logs.record()
        with sqlite3.connect(self.root/'events.sqlite3') as db:
            raw=json.loads(db.execute('SELECT payload FROM events').fetchone()[0])
            raw.update(secret=SECRET, tool=SECRET, code=SECRET, exception=SECRET,
                       frames=[{'file':SECRET,'line':3}], build=SECRET)
            db.execute('UPDATE events SET payload=?',(json.dumps(raw),))
        db.close()
        self.assertNotIn(SECRET.encode(), logs.export())

    def test_disk_full_does_not_replace_error_or_allow_retry(self):
        problem=failure(TimeoutError(SECRET),write_may_have_run=True)
        with patch.object(logs.sqlite3,'connect',side_effect=OSError('disk full '+SECRET)):
            value=logs.attach(problem)
            self.assertFalse(value['diagnostic_saved'])
            self.assertEqual(value['state'],'needs_verification')
            self.assertFalse(value['recovery']['retry_write_allowed'])
            self.assertFalse(logs.report()['available'])
        self.assertNotIn(SECRET,json.dumps(value))

    def test_concurrent_writers_preserve_counts(self):
        threads=[threading.Thread(target=lambda:[logs.record(code='timeout') for _ in range(5)]) for _ in range(4)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(logs.report()['events'][0]['count'],20)

    def test_clear_preserves_business_files(self):
        keep=Path(self.tmp.name)/'task-receipt.txt';keep.write_text(SECRET)
        logs.record();logs.clear()
        self.assertEqual(logs.report()['events'],[])
        self.assertEqual(keep.read_text(),SECRET)

    def test_corrupt_or_oversized_log_can_be_cleared_and_recovers(self):
        self.root.mkdir()
        db=self.root/'events.sqlite3'
        db.write_bytes(b'corrupt synthetic log')
        self.assertFalse(logs.record()['diagnostic_saved'])
        self.assertFalse(logs.report()['available'])
        logs.clear()
        self.assertTrue(logs.record()['diagnostic_saved'])
        with patch.object(logs,'MAX_DATABASE_BYTES',1):
            self.assertFalse(logs.report()['available'])
            logs.clear()
        self.assertTrue(logs.record()['diagnostic_saved'])

    def test_diagnostic_symlink_never_touches_target(self):
        target=Path(self.tmp.name)/'keep.txt';target.write_text(SECRET)
        self.root.mkdir()
        try:(self.root/'events.sqlite3').symlink_to(target)
        except OSError:self.skipTest('symlink privilege unavailable')
        self.assertFalse(logs.record()['diagnostic_saved'])
        with self.assertRaises(ValueError):logs.clear()
        self.assertEqual(target.read_text(),SECRET)

    def test_mcp_exception_record_and_original_cause_survive(self):
        mcp=FastMCP('synthetic')
        @mcp.tool(name='school_web_read')
        async def explode() -> dict:
            raise TimeoutError(SECRET)
        logs.install(mcp,'web');logs.install(mcp,'web')
        with self.assertRaises(Exception) as error:
            asyncio.run(mcp.call_tool('school_web_read',{}))
        attached=logs.attach(failure(error.exception),exc=error.exception)
        event=logs.report()['events'][0]
        self.assertEqual(attached['error_id'],event['error_id'])
        self.assertEqual(event['outcome'],'read_failed')
        self.assertEqual(event['code'],'timeout')
        self.assertEqual(event['count'],1)
        self.assertNotIn(SECRET.encode(),logs.export())

    def test_mcp_returned_failure_text_matches_structured_id(self):
        from typing import Any
        mcp=FastMCP('synthetic')
        @mcp.tool(name='school_web_read')
        async def failed() -> dict[str,Any]:
            return {'state':'timeout'}
        logs.install(mcp,'web')
        text,value=asyncio.run(mcp.call_tool('school_web_read',{}))
        self.assertEqual(json.loads(text[0].text),value)
        self.assertIn('error_id',value)
        self.assertEqual(logs.report()['events'][0]['count'],1)


@unittest.skipUnless(os.name=='nt','Desktop DPAPI')
class DesktopLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        env=patch.dict(os.environ);env.start();self.addCleanup(env.stop)
        from school_mcp.desktop.app import Application
        self.app=Application(self.tmp.name)

    def test_model_failure_has_same_visible_id_and_log(self):
        from school_mcp.desktop.model import ModelError
        def bad():raise ModelError('model_rate_limited','固定错误说明')
        self.app.start('model_test',bad);self.app.worker.join(10)
        self.assertEqual(self.app.operation['state'],'failed')
        self.assertEqual(self.app.operation['error_id'],logs.report()['events'][0]['error_id'])
        self.assertEqual(logs.report()['events'][0]['code'],'model_rate_limited')

    def test_handled_invalid_document_is_logged_without_name_or_content(self):
        from school_mcp.desktop import materials
        directory=self.app.data.profile/'uploads'/'synthetic';directory.mkdir(parents=True)
        path=directory/(SECRET+'.docx');path.write_bytes(SECRET.encode())
        value=materials.call(self.app.data,{'allowed_files':[str(path.resolve())]},'school_local_read_file',{'path':str(path)})
        self.assertEqual(value['status'],'unreadable_file')
        self.assertIn('error_id',value)
        self.assertNotIn(SECRET.encode(),logs.export())
        self.assertEqual(logs.report()['events'][0]['stage'],'document')

    def test_startup_failure_creates_safe_log_without_real_dialog(self):
        from school_mcp.desktop.entry import main
        with patch('school_mcp.desktop.server.main',side_effect=PermissionError(SECRET)), \
             patch('ctypes.windll.user32.MessageBoxW') as dialog, patch('sys.argv',['school-agent']):
            with self.assertRaises(SystemExit):main()
        self.assertNotIn(SECRET,str(dialog.call_args))
        self.assertEqual(logs.report()['events'][0]['code'],'startup_failed')
        self.assertIn(logs.report()['events'][0]['error_id'],str(dialog.call_args))

    def test_persistence_failure_reported_without_reexecuting_function(self):
        from school_mcp.desktop import app as module
        original=module.write_private
        count=[0];calls=[0]
        def fail_final(*a,**kw):
            count[0]+=1
            if count[0]>1:raise OSError(SECRET)
            return original(*a,**kw)
        def work():calls[0]+=1;return {'state':'completed'}
        with patch.object(module,'write_private',side_effect=fail_final):
            self.app.start('chat',work);self.app.worker.join(10)
        self.assertEqual(calls[0],1)
        self.assertEqual(self.app.operation['state'],'failed')
        self.assertEqual(logs.report()['events'][0]['code'],'storage_failed')
        self.assertNotIn(SECRET.encode(),logs.export())

    def test_interrupted_operation_survives_restart_and_clear(self):
        from school_mcp.desktop.storage import write_private
        from school_mcp.desktop.app import Application
        write_private(self.app.data.root/'operation.dpapi',{'state':'running'})
        other=Application(self.tmp.name)
        self.assertEqual(other.operation['state'],'interrupted')
        self.assertIn('error_id',other.operation)
        self.assertEqual(logs.report()['events'][0]['outcome'],'interrupted')
        logs.clear()
        self.assertTrue((other.data.root/'operation.dpapi').exists())

    def test_authenticated_export_prelogin_and_cross_origin_rejection(self):
        from school_mcp.desktop.server import Server
        logs.record(exc=RuntimeError(SECRET))
        with Server(self.app) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with httpx.Client(base_url=server.origin,trust_env=False) as client:
                    self.assertEqual(client.get('/api/diagnostics').status_code,403)
                    headers={'X-School-Token':server.token,'Origin':server.origin}
                    report=client.get('/api/diagnostics',headers=headers)
                    self.assertEqual(report.status_code,200)
                    self.assertNotIn(SECRET,report.text)
                    exported=client.get('/api/diagnostics/export',headers=headers)
                    self.assertEqual(exported.status_code,200)
                    bad=client.post('/api/diagnostics/clear',headers={**headers,'Origin':'https://evil.invalid'},json={'confirmation':'diagnostics'})
                    self.assertEqual(bad.status_code,403)
                    self.assertTrue(logs.report()['events'])
                    good=client.post('/api/diagnostics/clear',headers=headers,json={'confirmation':'diagnostics'})
                    self.assertEqual(good.status_code,200)
                    self.assertFalse(logs.report()['events'])
            finally:
                server.shutdown();thread.join(5)

    def test_ui_error_endpoint_never_logs_payload(self):
        result=self.app.api('/api/diagnostics/ui-error',{'message':SECRET,'stack':SECRET,'url':SECRET})
        self.assertIn('error_id',result)
        self.assertNotIn(SECRET.encode(),logs.export())


if __name__=='__main__':unittest.main()
