import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import MagicMock,patch
from xml.etree import ElementTree as ET

from school_mcp.young import registration as r,worker as w,scheduler as s
from test_young_registration import activity


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.jobs=r.Jobs('synthetic-account',Path(self.temp.name))
        self.due=datetime.now(timezone.utc)+timedelta(minutes=3)
        self.job=self.jobs.create('合成测试活动',self.due.isoformat(),authorized=True,
            monitor_from=(self.due-timedelta(minutes=2)).isoformat())
        self.now=self.due-timedelta(seconds=1)
        self.detail=activity(self.due)

    def execute(self,details,*,timeout=False,cancel=False,registered=True):
        browser=MagicMock();browser.open.return_value.__enter__.return_value=browser
        browser.locate.side_effect=details
        browser.registration_button_ready.return_value=True
        browser.detail.return_value={**self.detail,'booleanRegistration':int(registered)}
        def submit(item,cb):
            self.assertGreaterEqual(self.now,self.due)
            cb()
            if timeout:raise TimeoutError()
            return {'success':True}
        browser.submit.side_effect=submit
        def sleep(seconds):
            self.now+=timedelta(seconds=seconds)
            if cancel:self.jobs.cancel(self.job['job_id'])
        with patch.object(w,'Jobs',return_value=self.jobs),patch.object(w,'load_session',return_value={'account':'synthetic-account'}),patch.object(w,'RegistrationBrowser',return_value=browser):
            value=w.monitor_job(self.job['job_id'],sleep=sleep,clock=lambda:self.now)
        return value,browser

    def test_absent_then_opens_script_submits_and_verifies(self):
        value,browser=self.execute([None,self.detail])
        self.assertEqual(value['state'],'verified');self.assertEqual(browser.submit.call_count,1)
        self.assertEqual(browser.detail.call_count,1)

    def test_no_default_fifteen_minute_limit(self):
        self.assertIsNone(self.job['expires_at'])
        self.now=self.due+timedelta(minutes=20)
        value,_=self.execute([self.detail])
        self.assertEqual(value['state'],'verified')

    def test_wait_for_platform_state_not_clock_only(self):
        notready={**self.detail,'applyStatus':25}
        value,browser=self.execute([notready,self.detail])
        self.assertEqual(value['state'],'verified');self.assertEqual(browser.locate.call_count,2)

    def test_cancel_wait_without_write(self):
        value,browser=self.execute([None],cancel=True)
        self.assertEqual(value['state'],'cancelled');browser.submit.assert_not_called()

    def test_timeout_stops_with_no_replay(self):
        self.now=self.due
        value,browser=self.execute([self.detail],timeout=True)
        self.assertEqual(value['state'],'uncertain');self.assertEqual(browser.submit.call_count,1)
        again,other=self.execute([])
        other.open.assert_not_called();self.assertEqual(again['state'],'uncertain')

    def test_closed_registration_stops(self):
        self.now=self.due+timedelta(hours=2)
        value,browser=self.execute([self.detail])
        self.assertEqual(value['state'],'expired');browser.submit.assert_not_called()

    def test_extra_required_information_stops(self):
        self.now=self.due
        value,browser=self.execute([{**self.detail,'needSignInfo':'1'}])
        self.assertEqual(value['state'],'stopped');browser.submit.assert_not_called()

    def test_cooldown_wait_then_resume(self):
        value,browser=self.execute([r.network.CooldownError(31),self.detail])
        self.assertGreaterEqual(self.now,self.due+timedelta(seconds=30))
        self.assertEqual(value['state'],'verified')

    def test_server_success_without_record_is_uncertain(self):
        self.now=self.due
        value,_=self.execute([self.detail],registered=False)
        self.assertEqual(value['state'],'uncertain')


class SchedulerTests(unittest.TestCase):
    def test_native_xml_launches_python_not_ai(self):
        raw=s.task_xml('a'*32,'2030-01-10T14:58:00+08:00')
        root=ET.fromstring(raw);ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertTrue(root.find('.//t:Command',ns).text.endswith('pythonw.exe'))
        self.assertIn('young-registration-worker',root.find('.//t:Arguments',ns).text)
        self.assertEqual(root.find('.//t:StartBoundary',ns).text,'2030-01-10T14:58:00+08:00')
        self.assertEqual(root.find('.//t:ExecutionTimeLimit',ns).text,'PT0S')
        self.assertEqual(root.find('.//t:LogonType',ns).text,'InteractiveToken')

    def test_schedule_ids_cannot_inject_commands_or_paths(self):
        for bad in ['../../abc',"a';Remove-Item",'',None]:
            with self.assertRaises(r.YoungError):s.task_name(bad)


class FuzzyTests(unittest.TestCase):
    def setUp(self):
        self.job=dict(exact_name=None,name_keywords=['测试校区','2030','合成','讲座'],opens_at='2030-01-10T15:00:00+08:00',item_id=None)

    def test_normalizes_punctuation_spaces_and_fullwidth(self):
        self.assertTrue(r.matches_name({'itemName':'（测试校区）２０３０年“合成 · 讲座”'},self.job))
        self.assertFalse(r.matches_name({'itemName':'（其他校区）2030年合成讲座'},self.job))

    def test_no_partial_word_count_or_regex_matching(self):
        self.assertFalse(r.matches_name({'itemName':'测试校区2030讲座'},self.job))
        self.assertFalse(r.matches_name({'itemName':'测试校区合成讲座'},self.job))

    def test_create_and_read_encrypted_keyword_job(self):
        with tempfile.TemporaryDirectory() as directory:
            store=r.Jobs('synthetic',Path(directory));due=datetime.now(timezone.utc)+timedelta(days=1)
            v=store.create(None,due.isoformat(),authorized=True,name_keywords=self.job['name_keywords'])
            self.assertEqual(v['name_keywords'],self.job['name_keywords'])
            with self.assertRaises(r.YoungError):store.create(None,due.isoformat(),authorized=True,name_keywords=['讲座'])

    def test_locate_filters_by_open_time_and_reports_ambiguity(self):
        browser=r.RegistrationBrowser({})
        rows=[dict(id='one',itemName='测试校区2030合成讲座甲'),dict(id='two',itemName='测试校区2030合成讲座乙')]
        browser.listing=MagicMock(return_value=rows)
        browser.detail=lambda identifier:{**next(x for x in rows if x['id']==identifier),'applySt':self.job['opens_at']}
        with self.assertRaises(r.AmbiguousMatch) as error:browser.locate(self.job,allow_missing=True)
        self.assertEqual(len(error.exception.candidates),2)
        browser.detail=lambda identifier:{**rows[0],'applySt':'2030-01-11T15:00:00+08:00'}
        self.assertIsNone(browser.locate(self.job,allow_missing=True))

    def test_discovered_target_cannot_switch(self):
        job={**self.job,'resolved_name':'测试校区2030合成讲座甲'}
        self.assertFalse(r.matches_name({'itemName':'测试校区2030合成讲座乙'},job))


class SessionWarmupTests(unittest.TestCase):
    def test_valid_session_does_not_reconnect(self):
        with patch('school_mcp.young.client.YoungClient') as client,patch('school_mcp.young.reconnect.start') as start:
            w.ensure_session();client.return_value.check.assert_called_once();start.assert_not_called()

    def test_one_recovery_then_connected(self):
        with patch('school_mcp.young.client.YoungClient') as client,patch('school_mcp.young.reconnect.start',return_value={'started':True}) as start,patch('school_mcp.young.reconnect.status',return_value={'login_progress':{'stage':'connected'}}):
            client.return_value.check.side_effect=r.YoungError('会话失效')
            w.ensure_session();start.assert_called_once()

    def test_manual_verification_stops_without_repeated_login(self):
        with patch('school_mcp.young.client.YoungClient') as client,patch('school_mcp.young.reconnect.start',return_value={'started':True}) as start,patch('school_mcp.young.reconnect.status',return_value={'login_progress':{'stage':'waiting_for_verification'}}):
            client.return_value.check.side_effect=r.YoungError('会话失效')
            with self.assertRaises(r.YoungError):w.ensure_session()
            start.assert_called_once()


if __name__=='__main__':unittest.main()
