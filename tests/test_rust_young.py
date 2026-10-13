"""Real Rust pipe tests; synthetic projects, no school calls or actual schedules."""
import concurrent.futures
from contextlib import closing
import json
import os
import sqlite3
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from school_mcp.desktop import native_core
from school_mcp.young import native, registration as r, worker as w

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get('SCHOOL_TEST_CORE', ROOT / 'native/core/target/debug/school-core.exe'))


def activity(opens):
    return dict(id='synthetic-event', itemName='合成测试活动', applySt=opens.isoformat(),
        applyEt=(opens+timedelta(hours=1)).isoformat(), booleanRegistration=0,
        itemCategory='0', form='0', needSignInfo='0', onlineStatus=0, marathon='0',
        signInfo=0, examineStatus=10, applyStatus=26, baseContent='线下报告', conceive='')


@unittest.skipUnless(os.name == 'nt' and CORE.is_file(), 'Windows built Rust core required')
class RustYoungTests(unittest.TestCase):
    def setUp(self):
        native_core.shutdown()
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory=Path(tmp.name)
        env=patch.dict(os.environ, {'SCHOOL_NATIVE_CORE':str(CORE), 'SCHOOL_MCP_LOCAL_DIR':tmp.name})
        env.start(); self.addCleanup(env.stop); self.addCleanup(native_core.shutdown)
        self.due=datetime.now(timezone.utc)+timedelta(minutes=5)
        self.store=r.Jobs('synthetic-account', self.directory)
        self.job=self.store.create('合成测试活动', self.due.isoformat(), authorized=True)
        self.detail=activity(self.due)

    def direct(self, **values):
        raw=json.dumps({'operation':'young_job','input':values}).encode()
        process=subprocess.Popen([str(CORE),'--stdio'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            out,_=process.communicate(struct.pack('>I',len(raw))+raw,timeout=15)
        finally:
            if process.poll() is None:
                process.kill();process.communicate()
        self.assertEqual(process.returncode,0)
        size=struct.unpack('>I',out[:4])[0]
        self.assertEqual(size,len(out)-4)
        return json.loads(out[4:])

    def test_old_python_encrypted_rows_are_native_readable_and_dedupe(self):
        store=r.Jobs('synthetic-account',self.directory/'legacy')
        with patch.object(native,'enabled',return_value=False):
            legacy=store.create('旧版合成测试',self.due.isoformat(),authorized=True)
        loaded=store.view(legacy['job_id'])
        self.assertEqual(loaded,legacy)
        self.assertEqual(store.create('旧版合成测试',self.due.isoformat(),authorized=True)['job_id'],legacy['job_id'])

    def test_native_rows_and_results_are_python_readable(self):
        self.store.claim(self.job['job_id'],self.due)
        self.store.transition(self.job['job_id'],'checking','submitting',{'item_id':self.detail['id']})
        native_view=self.store.view(self.job['job_id'])
        with patch.object(native,'enabled',return_value=False):
            self.assertEqual(self.store.view(self.job['job_id']),native_view)
        raw=self.store.path.read_bytes()
        self.assertNotIn('合成测试活动'.encode(),raw)
        self.assertNotIn(b'synthetic-account',raw)

    def test_unknown_result_is_permanent_no_replay(self):
        self.store.claim(self.job['job_id'],self.due)
        self.store.transition(self.job['job_id'],'checking','submitting',{'item_id':self.detail['id']})
        self.store.transition(self.job['job_id'],'submitting','uncertain',{'item_id':self.detail['id']})
        self.assertFalse(self.store.claim(self.job['job_id'],self.due))
        self.assertFalse(self.store.claim_monitor(self.job['job_id'],self.due))
        for next_state in ['scheduled','checking','monitoring','cancelled','submitting']:
            with self.assertRaises(r.YoungError):
                self.store.transition(self.job['job_id'],'uncertain',next_state)
        with self.assertRaises(r.YoungError):self.store.cancel(self.job['job_id'])
        self.assertEqual(self.store.create('合成测试活动',self.due.isoformat(),authorized=True)['job_id'],self.job['job_id'])

    def test_two_rust_processes_claim_once(self):
        args=dict(action='claim',database=str(self.store.path),account=self.store.account,
                  job_id=self.job['job_id'],now=self.due.isoformat())
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.direct(**args),range(2)))
        self.assertTrue(all(x['ok'] for x in results),results)
        self.assertEqual(sum(bool(x['result']) for x in results),1)

    def test_two_distinct_jobs_cannot_submit_same_activity(self):
        second=self.store.create(None,self.due.isoformat(),authorized=True,name_keywords=['合成','活动'])
        for job in [self.job,second]:self.store.claim(job['job_id'],self.due)
        def submit(job):
            return self.direct(action='transition',database=str(self.store.path),account=self.store.account,
                job_id=job['job_id'],expected='checking',state='submitting',result={'item_id':self.detail['id']})
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(submit,[self.job,second]))
        self.assertEqual(sum(x['ok'] for x in results),1,results)
        states=[self.store.view(j['job_id'])['state'] for j in [self.job,second]]
        winner=[self.job,second][states.index('submitting')]
        loser=[self.job,second][states.index('checking')]
        self.store.transition(winner['job_id'],'submitting','uncertain',{'item_id':self.detail['id']})
        with self.assertRaises(r.YoungError):
            self.store.transition(loser['job_id'],'checking','submitting',{'item_id':self.detail['id']})
        raw=self.store.path.read_bytes()
        self.assertNotIn(self.detail['id'].encode(),raw)

    def test_legacy_uncertain_and_verified_receipts_block_new_task(self):
        for state,result in [('uncertain',{'item_id':'synthetic-old-unknown'}),
                             ('verified',{'item':{'id':'synthetic-old-verified'}})]:
            store=r.Jobs('synthetic-account',self.directory/state)
            item=result.get('item_id') or result['item']['id']
            with patch.object(native,'enabled',return_value=False):
                old=store.create('旧版不同活动'+state,self.due.isoformat(),authorized=True)
                store.claim(old['job_id'],self.due)
                store.transition(old['job_id'],'checking','submitting',{'item_id':item})
                store.transition(old['job_id'],'submitting',state,result)
            new=store.create('新版不同关键词'+state,self.due.isoformat(),authorized=True)
            store.claim(new['job_id'],self.due)
            with self.assertRaises(r.YoungError):
                store.transition(new['job_id'],'checking','submitting',{'item_id':item})

    def test_legacy_adapter_can_read_but_cannot_write_migrated_database(self):
        with patch.object(native,'enabled',return_value=False):
            self.assertEqual(self.store.view(self.job['job_id'])['state'],'scheduled')
            with self.assertRaises(r.YoungError):self.store.claim(self.job['job_id'],self.due)
            with self.assertRaises(r.YoungError):self.store.create('兼容层不能写入',self.due.isoformat(),authorized=True)
        self.assertEqual(self.store.view(self.job['job_id'])['state'],'scheduled')

    def test_database_trigger_blocks_already_running_old_worker(self):
        self.store.claim(self.job['job_id'],self.due)
        with closing(sqlite3.connect(self.store.path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE jobs SET state='submitting' WHERE id=?",[self.job['job_id']])
        self.store.transition(self.job['job_id'],'checking','submitting',{'item_id':self.detail['id']})
        self.store.transition(self.job['job_id'],'submitting','uncertain',{'item_id':self.detail['id']})
        with closing(sqlite3.connect(self.store.path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE jobs SET state='scheduled' WHERE id=?",[self.job['job_id']])

    def test_scheduler_restores_verified_runtime_without_inherited_env(self):
        spec={'native_runtime':native.runtime_spec()}
        native_core.shutdown()
        with patch.dict(os.environ,{},clear=False):
            del os.environ['SCHOOL_NATIVE_CORE']
            native.restore_runtime(spec)
            self.assertEqual(os.environ['SCHOOL_NATIVE_CORE'],str(CORE.resolve()))
            self.assertEqual(self.store.view(self.job['job_id'])['state'],'scheduled')
        wrong={'native_runtime':{**spec['native_runtime'],'sha256':'0'*64}}
        with self.assertRaises(r.YoungError):native.restore_runtime(wrong)
        with self.assertRaises(r.YoungError):native.restore_runtime({'native_runtime':{**spec['native_runtime'],'path':str(self.directory/'missing.exe')}})

    def test_worker_missing_core_stops_before_session_or_job(self):
        from school_mcp.young import scheduler
        spec={'native_runtime':{'required':True,'path':str(self.directory/'missing.exe')},'monitor_from':self.due.isoformat()}
        with patch.object(scheduler,'read_spec',return_value=spec),patch.object(scheduler,'write_spec') as saved,patch.object(w,'ensure_session') as session,patch.object(w,'monitor_job') as monitor:
            w.run_schedule('a'*32)
        session.assert_not_called();monitor.assert_not_called()
        self.assertEqual(saved.call_args.args[1]['worker_state'],'stopped')

    def test_old_python_entry_cannot_run_a_pinned_native_plan(self):
        from school_mcp.young import scheduler
        spec={'native_runtime':native.runtime_spec(),'monitor_from':self.due.isoformat()}
        with patch.object(scheduler,'read_spec',return_value=spec),patch.object(scheduler,'write_spec') as saved,patch.object(w,'ensure_session') as session,patch.object(w,'monitor_job') as monitor:
            w.run_schedule('a'*32)
        session.assert_not_called();monitor.assert_not_called()
        self.assertEqual(saved.call_args.args[1]['worker_state'],'stopped')
        self.assertIn('旧 Python',saved.call_args.args[1]['reason'])

    def test_schedule_spec_captures_core_without_registering_real_task(self):
        from school_mcp.young import scheduler
        start=self.due-timedelta(minutes=2)
        installed={'enabled':True,'start_boundary':start.isoformat()}
        with patch.object(native,'io',return_value=installed) as io,patch.object(scheduler,'powershell') as ps,patch.object(scheduler,'write_spec') as write:
            self.assertEqual(scheduler.install_schedule(start.isoformat(),self.due.isoformat(),job_id=self.job['job_id']),installed)
        io.assert_called_once_with('install_schedule',monitor_from=start.isoformat(),opens_at=self.due.isoformat(),job_id=self.job['job_id'],schedule_id=None)
        ps.assert_not_called();write.assert_not_called()

    def test_native_task_xml_and_cli_never_start_python(self):
        from xml.etree import ElementTree as ET
        identifier='c'*32
        output=native.io('schedule_xml',schedule_id=identifier,monitor_from=self.due.isoformat())
        document=ET.fromstring(output['xml']);ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertEqual(Path(document.find('.//t:Command',ns).text).resolve(),CORE.resolve())
        arguments=document.find('.//t:Arguments',ns).text
        self.assertIn('--young-worker --schedule-id '+identifier,arguments)
        self.assertNotIn('python',arguments.lower())
        self.assertEqual(document.find('.//t:LogonType',ns).text,'InteractiveToken')
        self.assertEqual(document.find('.//t:RunLevel',ns).text,'LeastPrivilege')

    def test_real_worker_cli_stops_changed_runtime_before_account_access(self):
        identifier='d'*32
        directory=self.directory/'isolated-cli';plans=directory/'young-schedules';plans.mkdir(parents=True)
        path=plans/(identifier+'.json')
        path.write_text(json.dumps({'schedule_id':identifier,'executor':'rust','cancelled':False,
            'native_runtime':{**native.runtime_spec(),'sha256':'0'*64}}),encoding='utf-8')
        result=subprocess.run([str(CORE),'--young-worker','--schedule-id',identifier,'--private-dir',str(directory)],
            capture_output=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,1)
        self.assertEqual(result.stdout,b'');self.assertEqual(result.stderr,b'')
        saved=json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['worker_state'],'stopped')
        self.assertFalse((directory/'young-registrations.sqlite3').exists())

    def test_cross_account_and_stale_transition_rejected(self):
        other=r.Jobs('synthetic-other',self.directory)
        with self.assertRaises(r.YoungError):other.view(self.job['job_id'])
        with self.assertRaises(r.YoungError):other.claim(self.job['job_id'],self.due)
        self.assertTrue(self.store.claim(self.job['job_id'],self.due))
        with self.assertRaises(r.YoungError):self.store.transition(self.job['job_id'],'monitoring','submitting')

    def test_authorization_time_and_fuzzy_validation(self):
        for kwargs in [dict(authorized=False),dict(authorized=True,poll_seconds=True),
                       dict(authorized=True,poll_seconds=9),dict(authorized=True,item_id='../escape')]:
            with self.assertRaises(r.YoungError):self.store.create('新合成活动',self.due.isoformat(),**kwargs)
        with self.assertRaises(r.YoungError):self.store.create('新合成活动',self.due.replace(tzinfo=None).isoformat(),authorized=True)
        with self.assertRaises(r.YoungError):self.store.create(None,self.due.isoformat(),authorized=True,name_keywords=['测试','测试'])
        with self.assertRaises(r.YoungError):self.store.create('截断名称…',self.due.isoformat(),authorized=True)

    def test_expiry_and_early_gate(self):
        self.assertFalse(self.store.claim(self.job['job_id'],self.due-timedelta(seconds=1)))
        self.assertFalse(self.store.claim(self.job['job_id'],self.due+timedelta(minutes=16)))
        self.assertEqual(self.store.view(self.job['job_id'])['state'],'expired')

    def test_unicode_match_and_ambiguous_target(self):
        job=dict(exact_name=None,name_keywords=['测试校区','2030','合成','讲座'],opens_at=self.due.isoformat(),item_id=None)
        rows=[dict(id='one',itemName='（测试校区）２０３０年“合成 · 讲座”',applySt=self.due.isoformat()),
              dict(id='two',itemName='测试校区2030合成讲座乙',applySt=self.due.isoformat())]
        self.assertTrue(r.matches_name(rows[0],job))
        found=native.policy('locate',records=rows,job=job)
        self.assertEqual(found['state'],'ambiguous')
        rows[1]['applySt']=(self.due+timedelta(days=1)).isoformat()
        self.assertEqual(native.policy('locate',records=rows,job=job)['state'],'unique')
        self.assertFalse(r.matches_name(rows[1],{**job,'resolved_name':rows[0]['itemName']}))

    def test_native_eligibility_matches_legacy_and_stops_extra_requirements(self):
        self.assertEqual(r.check_candidate(self.detail,self.job,self.due),'eligible')
        for key,value in [('needSignInfo','1'),('itemCategory','1'),('form','1'),('onlineStatus',1),
                          ('marathon',1),('signInfo',1),('booleanRegistration',None),
                          ('applyStatus',28),('baseContent','仅限某学院，需缴费')]:
            with self.subTest(key=key),self.assertRaises(r.YoungError):
                r.check_candidate({**self.detail,key:value},self.job,self.due)
        approved={**self.job,'allow_non_cancellable':True}
        self.assertEqual(r.check_candidate({**self.detail,'signInfo':1},approved,self.due),'eligible')

    def test_monitor_decisions_do_not_submit_before_open(self):
        before=self.due-timedelta(seconds=1)
        self.assertEqual(w.monitoring_decision(None,self.job,before),'waiting_for_project')
        self.assertEqual(w.monitoring_decision(self.detail,self.job,before),'waiting_for_open')
        self.assertEqual(w.monitoring_decision(self.detail,self.job,self.due),'eligible')
        self.assertEqual(w.monitoring_decision(self.detail,self.job,self.due+timedelta(hours=2)),'expired')
        with self.assertRaises(r.YoungError):native.policy('before_write',detail=self.detail,job=self.job,now=before.isoformat())

    def test_receipt_independent_status_and_target(self):
        args=dict(job=self.job,item_id=self.detail['id'],response={'success':True})
        result=native.policy('receipt',detail=self.detail,**args)
        self.assertEqual(result['state'],'uncertain')
        result=native.policy('receipt',detail={**self.detail,'booleanRegistration':1},**args)
        self.assertEqual(result['state'],'verified')
        self.assertFalse(result['result']['credit_hours_awarded'])
        with self.assertRaises(r.YoungError):native.policy('receipt',detail={**self.detail,'id':'other'},**args)

    def test_project_html_text_and_list_schema(self):
        detail={**self.detail,'baseContent':'<p>正文一</p><script>danger()</script><style>x{}</style><p>正文二</p>'}
        result=r.project_view(detail)
        self.assertEqual(result['baseContent'],'正文一\n正文二')
        self.assertNotIn('danger',result['baseContent'])
        self.assertEqual(native.parse('list',data={'success':True,'result':{'records':[detail]}}),[detail])
        for data in [{'success':False},{'success':True,'result':{}},{'success':True,'result':{'records':[{}]}}]:
            with self.assertRaises(r.YoungError):native.parse('list',data=data)
        with self.assertRaises(r.YoungError):native.parse('detail',item_id='other',data={'success':True,'result':detail})

    def test_native_io_failure_does_not_fallback(self):
        from school_mcp.desktop.model import ModelError
        with patch.object(native_core,'call',side_effect=ModelError('runtime_failure','合成核心失败')),patch.object(self.store,'db',side_effect=AssertionError('no Python fallback')):
            with self.assertRaises(r.YoungError):self.store.view(self.job['job_id'])

    def test_monitor_uses_rust_gate_and_receipt_with_fake_browser(self):
        job=self.store.create('另一个合成项目',self.due.isoformat(),authorized=True,
            monitor_from=(self.due-timedelta(minutes=2)).isoformat())
        detail={**self.detail,'itemName':'另一个合成项目'}
        browser=MagicMock();browser.open.return_value.__enter__.return_value=browser
        browser.locate.return_value=detail;browser.registration_button_ready.return_value=True
        browser.detail.return_value={**detail,'booleanRegistration':1}
        def submit(_item,callback):callback();return {'success':True}
        browser.submit.side_effect=submit
        with patch.object(w,'Jobs',return_value=self.store),patch.object(w,'load_session',return_value={'account':'synthetic-account'}),patch.object(w,'RegistrationBrowser',return_value=browser):
            result=w.monitor_job(job['job_id'],sleep=lambda _:None,clock=lambda:self.due)
        self.assertEqual(result['state'],'verified');browser.submit.assert_called_once()
        self.assertFalse(result['retry_write_allowed'])


if __name__=='__main__':unittest.main()
