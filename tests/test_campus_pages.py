"""Direct pages: account boundaries, no model dependency and no school writes."""
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from urllib.parse import urlsplit
import httpx

from school_mcp.desktop import campus
from school_mcp.desktop.app import Application
from school_mcp.desktop.server import Server
from school_mcp.desktop.storage import read_private
from school_mcp.bb.identity import save_session_identity

SCHEDULE={'semester':{'id':42,'name':'合成学期'},'current_week':2,'count':3,
    'activities':[{'course_name':'合成课程','weekday':1,'weeks':[1,2],'start':'08:00','end':'09:40'},
                  {'course_name':'合成其他课程','weekday':2,'weeks':[3],'start_period':3,'end_period':4},
                  {'course_name':'合成日期待核对','weekday':None,'weeks':[2],'self_defined_dates':True,'group':'合成分组'}]}
SEMESTERS={'current_semester_id':42,'semesters':[{'id':42,'name':'合成学期'},{'id':43,'name':'另一合成学期'}]}


@unittest.skipUnless(os.name=='nt','DPAPI')
class CampusPages(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        env=patch.dict(os.environ);env.start();self.addCleanup(env.stop)
        self.app=Application(tmp.name);save_session_identity('synthetic-account');self.data=self.app.data

    def test_profile_private_bound_and_never_in_status(self):
        blank=campus.profile(self.data)
        self.assertFalse(blank['values'])
        values={k:'' for k in campus.PROFILE_FIELDS};values.update(name='synthetic-person-private',email='synthetic@example.test')
        result=self.app.api('/api/campus/profile',{'values':values,'revision':0})
        self.assertEqual(result['revision'],1)
        self.assertNotIn(b'synthetic-person-private',(self.data.profile/'personal-profile.dpapi').read_bytes())
        self.assertNotIn('synthetic-person-private',str(self.app.status()))
        self.assertEqual(campus.profile(self.data)['values']['name'],values['name'])
        with patch.object(campus,'load_credentials',return_value={'username':'different-account'}):
            with self.assertRaises(ValueError):campus.profile(self.data)
        with self.assertRaises(ValueError):campus.save_profile(self.data,values,0)
        values['email']='invalid'
        with self.assertRaises(ValueError):campus.save_profile(self.data,values,1)
        self.assertEqual(campus.profile(self.data)['revision'],1)

    def test_profile_schema_rejects_unknown_secret_fields_and_wrong_types(self):
        values={k:'' for k in campus.PROFILE_FIELDS}
        for invalid in [values|{'password':'synthetic'}, values|{'name':None}, values|{'phone':'invalid'},
                        values|{'entry_month':'2026-13'}, values|{'college':'x\n'}, {'name':'only-name'}]:
            with self.assertRaises(ValueError):campus.save_profile(self.data,invalid,0)
        with self.assertRaises(ValueError):campus.save_profile(self.data,values,False)

    def test_timetable_preserves_cache_when_refresh_fails_or_account_changes(self):
        client=Mock();client.semesters.return_value=SEMESTERS;client.timetable.return_value=SCHEDULE
        with patch.object(campus,'JWClient',return_value=client):
            result=campus.refresh_timetable(self.data,0)
            self.assertFalse(result['model_used']);client.timetable.assert_called_once_with(42)
            before=(self.data.profile/'timetable-view.dpapi').read_bytes()
            client.timetable.side_effect=TimeoutError('synthetic')
            with self.assertRaises(TimeoutError):campus.refresh_timetable(self.data,42)
            self.assertEqual(before,(self.data.profile/'timetable-view.dpapi').read_bytes())
            with self.assertRaises(ValueError):campus.refresh_timetable(self.data,44)
        self.assertEqual(campus.timetable(self.data)['data'],SCHEDULE)
        with patch.object(campus,'load_credentials',return_value={'username':'different-account'}):
            with self.assertRaises(ValueError):campus.timetable(self.data)

    def test_lock_blocks_personal_api_and_catalog_has_only_static_https_entries(self):
        for s in campus.sites()['sites']:
            p=urlsplit(s['url']);self.assertEqual(p.scheme,'https');self.assertFalse(p.query or p.fragment or p.username)
        with Server(self.app) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with httpx.Client(base_url=server.origin,trust_env=False) as client:
                    self.assertEqual(client.get('/api/campus/profile').status_code,403)
                    headers={'X-School-Token':server.token,'Origin':server.origin}
                    self.assertEqual(client.get('/api/campus/profile',headers=headers).status_code,200)
                    self.assertEqual(client.post('/api/campus/profile',headers={**headers,'Origin':'https://other.test'},json={}).status_code,403)
                    (self.data.root/'locked').touch()
                    self.assertEqual(client.get('/api/campus/profile',headers=headers).status_code,400)
                    self.assertEqual(client.get('/api/campus/timetable',headers=headers).status_code,400)
                    self.assertEqual(client.get('/api/campus/sites',headers=headers).status_code,200)
                    self.assertEqual(client.post('/api/campus/profile',headers=headers,json={}).status_code,400)
            finally:server.shutdown();thread.join(5)

    def test_timetable_workflow_does_not_require_model_or_write_school(self):
        client=Mock();client.semesters.return_value=SEMESTERS;client.timetable.return_value=SCHEDULE
        with patch.object(campus,'JWClient',return_value=client),patch('school_mcp.desktop.model.ModelClient.complete',side_effect=AssertionError('model called')) as model:
            result=self.app.api('/api/campus/timetable/refresh',{'semester_id':42})
            self.app.worker.join(10)
            self.assertEqual(self.app.operation['state'],'completed');model.assert_not_called()
            self.assertEqual(set(name for name,_,_ in client.mock_calls),{'semesters','timetable'})
        self.assertTrue(campus.timetable(self.data)['cached'])
        self.assertNotIn('synthetic',str(read_private(self.data.root/'operation.dpapi').get('result',{}).get('activities','')))
