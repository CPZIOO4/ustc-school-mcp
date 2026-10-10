import inspect
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from school_mcp import task_recovery as api
from school_mcp.mail.credentials import _dpapi
from school_mcp.workflow_store import PlanStore
from school_mcp.bb.submissions import Store
from school_mcp.young.registration import Jobs
from school_mcp.script_jobs import JobStore


class TaskRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in [('local_dir', self.root), ('account', 'synthetic-owner')]:
            p = patch.object(api, name, return_value=value); p.start(); self.addCleanup(p.stop)
        p = patch('httpx.Client', side_effect=AssertionError('no network'))
        p.start(); self.addCleanup(p.stop)

    def plan(self, service='icourse', owner='synthetic-owner'):
        store = PlanStore(service, owner, self.root)
        view = store.create('synthetic-target', {'private':'synthetic-secret-content'}, {}, {}, {'title':'synthetic-private'})
        return store, view['plan_id']

    def detail(self, service, identifier, kind='primary'):
        result = api.query(service, identifier, kind)
        self.assertEqual(result['state'], 'completed', result)
        self.assertFalse(result['writes_performed'])
        return result['items'][0]

    def test_empty_does_not_create_database(self):
        for service in api.SERVICES:
            self.assertEqual(api.query(service)['items'], [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_account_service_isolation_pagination_and_missing(self):
        _, first = self.plan()
        other = PlanStore('icourse', 'synthetic-other', self.root)
        other.create('other', {}, {}, {}, {})
        self.plan('nan7')
        store = PlanStore('icourse', 'synthetic-owner', self.root)
        second = store.create('second', {}, {}, {}, {})['plan_id']
        page = api.query('icourse', limit=1)
        self.assertEqual(page['items'][0]['task_id'], second)
        self.assertEqual(page['next_offset'], 1)
        page2 = api.query('icourse', limit=1, offset=page['next_offset'])
        self.assertEqual(page2['items'][0]['task_id'], first)
        self.assertIsNone(page2['next_offset'])
        self.assertEqual(api.query('nan7', first)['state'], 'not_found')

    def test_read_only_and_minimal_output(self):
        store, identifier = self.plan()
        before = store.path.read_bytes()
        item = self.detail('icourse', identifier)
        self.assertEqual(item['stage'], 'prepared')
        self.assertEqual(item['next_step']['arguments'], {'plan_id':identifier,'verify':False})
        self.assertFalse(item['execution_authorized'])
        self.assertNotIn('synthetic-secret', json.dumps(item))
        self.assertEqual(before, store.path.read_bytes())

    def test_unknown_publish_outcome_uses_verification_not_publish(self):
        for service in ['icourse','nan7']:
            store, identifier = self.plan(service)
            sha = store.get(identifier)['sha256']
            store.claim(identifier, sha)
            store.finish(identifier, 'uncertain', {'offer_id':'synthetic-offer'})
            item = self.detail(service, identifier)
            self.assertEqual(item['stage'], 'needs_verification')
            self.assertTrue(item['next_step']['arguments']['verify'])
            self.assertFalse(item['retry_write_allowed'])

    def test_missing_remote_id_does_not_retry_nan7(self):
        store, identifier = self.plan('nan7')
        store.claim(identifier, store.get(identifier)['sha256'])
        store.finish(identifier, 'uncertain', {})
        item = self.detail('nan7', identifier)
        self.assertTrue(item['manual_review_required'])
        self.assertFalse(item['next_step']['arguments']['verify'])

    def test_expiry_is_derived_without_changing_record(self):
        store, identifier = self.plan()
        with store.db() as db:
            row = db.execute('SELECT payload FROM plans WHERE id=?',(identifier,)).fetchone()
            payload = api.decode(row[0]); payload['expires_at'] = '2000-01-01T00:00:00+00:00'
            db.execute('UPDATE plans SET payload=? WHERE id=?',(_dpapi(json.dumps(payload).encode()),identifier))
        self.assertEqual(self.detail('icourse', identifier)['reason_code'], 'preparation_expired')
        self.assertEqual(store.get(identifier)['state'], 'ready')

    def bb(self, phase='prepared', state='ready', due='2099-01-01T00:00:00+00:00'):
        store = Store(self.root)
        identifier = store.create({'account':'synthetic-owner','assignment':{'course_id':'c','content_id':'a'},
             'sha256':'synthetic-digest','prepared_at':datetime.now(timezone.utc).isoformat(),
             'baseline':{'due_at':due},'files':[{'data':'synthetic-private-attachment'}]})
        with store.db() as db:
            db.execute('UPDATE jobs SET state=?,phase=? WHERE id=?',(state,phase,identifier))
        return identifier

    def test_bb_deadline_and_write_phase(self):
        identifier = self.bb(due=None)
        self.assertEqual(self.detail('bb',identifier)['reason_code'],'deadline_unknown_or_late')
        store = Store(self.root)
        with store.db() as db:
            db.execute("UPDATE jobs SET state='uncertain',phase='new_attempt_requested' WHERE id=?",(identifier,))
        self.assertTrue(self.detail('bb',identifier)['manual_review_required'])
        with store.db() as db:
            db.execute("UPDATE jobs SET phase='submit_requested' WHERE id=?",(identifier,))
        self.assertTrue(self.detail('bb',identifier)['next_step']['arguments']['verify'])

    def young(self):
        store = Jobs('synthetic-young', self.root)
        with patch.object(api, 'account', return_value=store.account):
            job = store.create('合成报名项目', (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(), authorized=True)
        return store, job['job_id']

    def test_young_schedule_discovery_is_not_running_evidence(self):
        store, identifier = self.young()
        with patch.object(api,'account',return_value=store.account):
            self.assertEqual(self.detail('young',identifier)['reason_code'],'schedule_missing_ambiguous_or_truncated')
            folder=self.root/'young-schedules'; folder.mkdir()
            schedule='a'*32
            (folder/(schedule+'.json')).write_text(json.dumps({'job_id':identifier}),encoding='utf-8')
            item=self.detail('young',identifier)
            self.assertFalse(item['scheduler_checked'])
            self.assertEqual(item['next_step'],api.step('school_young_schedule_status',schedule_id=schedule))
            (folder/('b'*32+'.json')).write_text(json.dumps({'job_id':identifier}),encoding='utf-8')
            self.assertTrue(self.detail('young',identifier)['manual_review_required'])

    def test_young_unknown_result_and_terminal(self):
        store, identifier=self.young()
        store.transition(identifier,'scheduled','submitting',{'item_id':'synthetic-item'})
        with patch.object(api,'account',return_value=store.account):
            self.assertTrue(self.detail('young',identifier)['next_step']['arguments']['verify'])
            store.transition(identifier,'submitting','verified',{})
            item=self.detail('young',identifier)
            self.assertTrue(item['terminal']); self.assertIsNone(item['next_step'])

    def test_bb_script_pagination_filters_other_accounts_and_recovers_parent(self):
        store=JobStore(self.root); now=datetime.now(timezone.utc)
        args=(now.isoformat(),(now+timedelta(hours=1)).isoformat(),60)
        parent=self.bb()
        identifier=store.create('bb','bb_submit',{'preparation_id':parent},*args,'synthetic-owner')
        store.create('bb','bb_submit',{},*args,'synthetic-other')
        first=api.query('bb',task_kind='script',limit=1)
        self.assertEqual(first['items'],[]); self.assertEqual(first['next_offset'],1)
        second=api.query('bb',task_kind='script',limit=1,offset=1)
        self.assertEqual(second['items'][0]['task_id'],identifier)
        store.update(identifier,state='review',effect_started=True,
                     checkpoint={'operation':{'preparation_id':parent,'status_tool':'malicious-tool'}},
                     result={'reason_code':'arbitrary-private-text'})
        item=self.detail('bb',identifier,'script')
        self.assertEqual(item['stage'],'needs_verification')
        self.assertEqual(item['related_task']['arguments']['task_id'],parent)
        self.assertNotIn('malicious',json.dumps(item)); self.assertNotIn('arbitrary-private',json.dumps(item))

    def test_corrupt_identity_and_database_do_not_look_empty_success(self):
        with patch.object(api,'account',side_effect=RuntimeError('synthetic-secret')):
            self.assertEqual(api.query('bb')['state'],'identity_unavailable')
        (self.root/'bb-submissions.sqlite3').write_bytes(b'corrupt')
        self.assertEqual(api.query('bb')['state'],'records_unavailable')

    def test_invalid_arguments_never_access_storage(self):
        for arguments in [{'task_id':'../../file'}, {'limit':True}, {'offset':-1}, {'limit':51}]:
            with self.assertRaises(ValueError):api.query('bb',**arguments)
        with self.assertRaises(ValueError):api.query('young',task_kind='script')
        self.assertEqual(api.pagination(10000,10,True), {'next_offset':None,'scan_limit_reached':True})

    def test_next_tool_arguments_match_actual_servers(self):
        from school_mcp.bb.server import mcp as bb
        from school_mcp.young.server import mcp as young
        from school_mcp.icourse.server import mcp as icourse
        from school_mcp.nan7.server import mcp as nan7
        servers={'bb':bb,'young':young,'icourse':icourse,'nan7':nan7}
        for service in ['icourse','nan7']:
            _, identifier=self.plan(service)
            for item in [api.query(service)['items'][0],self.detail(service,identifier)]:
                instruction=item['next_step']
                tool=servers[service]._tool_manager.get_tool(instruction['tool'])
                inspect.signature(tool.fn).bind(**instruction['arguments'])
        for service,(name,key) in api.STATUS.items():
            inspect.signature(servers[service]._tool_manager.get_tool(name).fn).bind(**{key:'a'*32,'verify':False})


if __name__ == '__main__': unittest.main()
