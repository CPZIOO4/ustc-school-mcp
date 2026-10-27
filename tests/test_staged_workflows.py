import io
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile
from datetime import datetime, timezone, timedelta
from school_mcp import study_flow
from school_mcp.compute import experiments
from school_mcp.operation_records import Records


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        p=patch.dict(os.environ,{'SCHOOL_MCP_LOCAL_DIR':self.tmp.name});p.start();self.addCleanup(p.stop)
        p=patch('school_mcp.operation_records.load_credentials',return_value={'username':'testuser'});p.start();self.addCleanup(p.stop)

    def archive(self,names=None):
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as z:
            for name,content in (names or {'main.py':'print(1)'}).items():z.writestr(name,content)
        path=Path(self.tmp.name)/'experiment.zip';path.write_bytes(raw.getvalue());return path

    def test_archive_escape_and_oversized_resources_rejected(self):
        for name in ('../a.py','/a.py','C:/a.py','a/../b.py'):
            p=self.archive({name:'1','main.py':'1'})
            with self.assertRaises(ValueError):experiments.prepare([str(p)],'main.py')
        p=self.archive()
        with self.assertRaises(ValueError):experiments.prepare([str(p)],'main.py',gpus=2)

    def test_package_is_frozen_and_unknown_submit_is_never_replayed(self):
        path=self.archive();v=experiments.prepare([str(path)],'main.py')
        path.write_bytes(b'changed');r=Records('compute').read(v['experiment_id'])
        self.assertNotEqual(Path(r['package_path']).read_bytes(),b'changed')
        with patch.object(experiments,'preflight',return_value={'preflight':{'eligible':True}}),patch.object(experiments.transport,'execute',side_effect=TimeoutError) as call:
            with self.assertRaises(TimeoutError):experiments.submit(v['experiment_id'],v['archive_sha256'],True)
            again=experiments.submit(v['experiment_id'],v['archive_sha256'],True)
            self.assertEqual(again['state'],'unknown');self.assertEqual(call.call_count,1)

    def test_submit_authorization_and_hash_checked_before_network(self):
        v=experiments.prepare([str(self.archive())],'main.py')
        with patch.object(experiments.transport,'execute',side_effect=AssertionError('network')):
            self.assertEqual(experiments.submit(v['experiment_id'],v['archive_sha256'])['state'],'authorization_required')
            with self.assertRaises(ValueError):experiments.submit(v['experiment_id'],'wrong',True)

    def test_records_are_account_bound_and_claim_is_atomic(self):
        s=Records('compute');v=s.create({'state':'prepared'})
        self.assertIsNotNone(s.claim(v['id'],'submit'));self.assertIsNone(Records('compute').claim(v['id'],'submit'))
        with patch('school_mcp.operation_records.load_credentials',return_value={'username':'other'}):
            with self.assertRaises(ValueError):Records('compute').read(v['id'])

    def quick(self):
        today=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        entry={'course_id':'_1_1','content_id':'_2_1','title':'Lab1','entry_path':'/webapps/assignment/uploadAssignment?course_id=_1_1&content_id=_2_1&mode=view'}
        with patch.object(study_flow,'JWClient') as jw,patch.object(study_flow,'BBClient') as bb,patch.object(study_flow,'catalog',return_value={'assignments':[entry],'truncated':False}),patch.object(study_flow,'inspect_assignment',side_effect=AssertionError('history must not run')):
            jw.return_value.timetable.return_value={'current_week':3,'activities':[{'title':'真实字段测试','weeks':[3]}]}
            bb.return_value.courses.return_value={'courses':[{'course_id':'_1_1','title':'课程'}]}
            return study_flow.start(today,[{'course_keyword':'课程','term':'2026SP','assignment_keyword':'Lab1'}])

    def test_quick_result_has_pending_history_and_durable_link(self):
        v=self.quick();self.assertEqual(v['state'],'partial');self.assertEqual(v['assignments'][0]['state'],'pending')
        self.assertTrue(study_flow.status(v['query_id'])['assignments'][0]['link']['url'].endswith('mode=view'))
        with patch.object(study_flow,'inspect_assignment',return_value={'page_kind':'submission_history','submission_state':'historical_attempt_present'}) as inspect:
            done=study_flow.advance(v['query_id']);self.assertEqual(done['state'],'completed')
            study_flow.advance(v['query_id']);self.assertEqual(inspect.call_count,1)

    def test_error_and_cancel_keep_quick_result(self):
        v=self.quick()
        with patch.object(study_flow,'inspect_assignment',side_effect=TimeoutError):
            done=study_flow.advance(v['query_id']);self.assertEqual(done['state'],'completed_with_errors');self.assertIsNotNone(done['timetable'])
        v=self.quick();stop=threading.Event();stop.set()
        with patch.object(study_flow,'inspect_assignment',side_effect=AssertionError('stopped')):
            paused=study_flow.advance(v['query_id'],stop);self.assertEqual(paused['state'],'paused');self.assertEqual(paused['assignments'][0]['state'],'pending')

    def test_missing_scheduler_record_is_unknown_not_completed(self):
        v=experiments.prepare([str(self.archive())],'main.py');s=Records('compute');s.update(v['experiment_id'],submit_claim='x',job_id=123)
        s.update(v['experiment_id'],scheduler={'JobState':'RUNNING'})
        with patch.object(experiments.transport,'execute',return_value={'phase':'unknown','job_id':123}):
            result=experiments.status(v['experiment_id'])
            self.assertEqual(result['state'],'unknown');self.assertEqual(result['scheduler'],{})

    def test_download_bypasses_proxy_and_rejects_corrupt_result(self):
        v=experiments.prepare([str(self.archive())],'main.py');raw=self.archive({'metrics.json':'{}'}).read_bytes()
        for digest in (hashlib.sha256(raw).hexdigest(),'bad-digest'):
            with patch.object(experiments,'status',return_value=dict(v,state='completed')),patch.object(experiments.transport,'execute',return_value={'path':'/home/test/artifacts.zip','bytes':len(raw),'sha256':digest}),patch.object(experiments.transport,'cookies',return_value=('testuser',{})),patch.object(experiments.httpx,'Client') as client:
                response=client.return_value.__enter__.return_value.stream.return_value.__enter__.return_value
                response.iter_bytes.return_value=iter([raw])
                if digest=='bad-digest':
                    with self.assertRaises(ValueError):experiments.collect(v['experiment_id'])
                else:self.assertTrue(experiments.collect(v['experiment_id'])['collection_ready'])
                self.assertFalse(client.call_args.kwargs['trust_env'])
                self.assertEqual(list((Path(self.tmp.name)/'compute-downloads').glob('*.part')),[])

    def test_runner_allocates_output_and_setup_on_compute_node(self):
        v=experiments.prepare([str(self.archive({'main.py':'print(1)','setup.sh':'echo setup'}))],'main.py',arguments=['a; touch evil'])
        runner,batch=experiments.job_script(Records('compute').read(v['experiment_id']))
        compile(runner,'runner.py','exec');self.assertIn('SCHOOL_OUTPUT_DIR',runner);self.assertIn('receipt.json',runner);self.assertNotIn('touch evil',batch)

if __name__=='__main__':unittest.main()
