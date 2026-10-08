import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from school_mcp.young import registration as r


def activity(opens):
    return dict(id='synthetic-event',itemName='合成测试活动',applySt=opens.isoformat(),
        applyEt=(opens+timedelta(hours=1)).isoformat(),booleanRegistration=0,
        itemCategory='0',form='0',needSignInfo='0',onlineStatus=0,marathon='0',
        signInfo=0,examineStatus=10,applyStatus=26,baseContent='线下报告',conceive='')


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.jobs=r.Jobs('synthetic-account',Path(self.tmp.name))
        self.due=datetime.now(timezone.utc)+timedelta(minutes=5)
        self.job=self.jobs.create('合成测试活动',self.due.isoformat(),authorized=True)

    def test_requires_authorization_and_full_name(self):
        for name,auth in [('合成测试…',True),('合成测试...',True),('完整测试项目',False)]:
            with self.assertRaises(r.YoungError):self.jobs.create(name,self.due.isoformat(),authorized=auth)

    def test_requires_explicit_timezone_and_future(self):
        for due in ['2026-10-09 15:00:00',(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()]:
            with self.assertRaises(r.YoungError):self.jobs.create('完整测试项目',due,authorized=True)

    def test_idempotent_create_and_early_run(self):
        second=self.jobs.create('合成测试活动',self.due.isoformat(),authorized=True)
        self.assertEqual(second['job_id'],self.job['job_id'])
        self.assertFalse(self.jobs.claim(self.job['job_id']))
        self.assertEqual(self.jobs.view(self.job['job_id'])['state'],'scheduled')

    def test_concurrent_claim_at_most_one(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.jobs.claim(self.job['job_id'],self.due),range(4)))
        self.assertEqual(sum(results),1)

    def test_expiry_and_cancel(self):
        self.assertFalse(self.jobs.claim(self.job['job_id'],self.due+timedelta(minutes=16)))
        self.assertEqual(self.jobs.view(self.job['job_id'])['state'],'expired')
        other=self.jobs.create('另一个测试项目',self.due.isoformat(),authorized=True)
        self.jobs.cancel(other['job_id'])
        self.assertFalse(self.jobs.claim(other['job_id'],self.due))

    def test_cross_account_denied_and_payload_encrypted(self):
        other=r.Jobs('other-account',Path(self.tmp.name))
        with self.assertRaises(r.YoungError):other.view(self.job['job_id'])
        data=self.jobs.path.read_bytes()
        self.assertNotIn('合成测试活动'.encode(),data)
        self.assertNotIn(b'synthetic-account',data)

    def test_unknown_write_never_claims_again(self):
        self.jobs.claim(self.job['job_id'],self.due)
        self.jobs.transition(self.job['job_id'],'checking','submitting',{'item_id':'synthetic-event'})
        self.jobs.transition(self.job['job_id'],'submitting','uncertain',{'item_id':'synthetic-event'})
        self.assertFalse(self.jobs.claim(self.job['job_id'],self.due))
        with self.assertRaises(r.YoungError):self.jobs.cancel(self.job['job_id'])


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc);self.due=self.now-timedelta(minutes=1)
        self.job=dict(exact_name='合成测试活动',opens_at=self.due.isoformat(),item_id=None,allow_non_cancellable=False)
        self.detail=activity(self.due)

    def test_ordinary_and_existing(self):
        self.assertEqual(r.check_candidate(self.detail,self.job,self.now),'eligible')
        self.detail['booleanRegistration']=1
        self.assertEqual(r.check_candidate(self.detail,self.job,self.now),'already_registered')

    def test_mismatch_and_extra_conditions_stop(self):
        for field,value in [('itemName','其他校区活动'),('needSignInfo','1'),('itemCategory','1'),
                            ('marathon','1'),('form','1'),('onlineStatus',1),('signInfo',1),
                            ('booleanRegistration',None),('applyStatus',28),('baseContent','仅限某学院，需缴费')]:
            with self.subTest(field=field):
                d={**self.detail,field:value}
                with self.assertRaises(r.YoungError):r.check_candidate(d,self.job,self.now)

    def test_wrong_open_time_and_expired_stop(self):
        for field,value in [('applySt',(self.due-timedelta(days=1)).isoformat()),('applyEt',(self.now-timedelta(seconds=1)).isoformat())]:
            with self.assertRaises(r.YoungError):r.check_candidate({**self.detail,field:value},self.job,self.now)

    def test_explicit_non_cancellable_consent(self):
        self.detail['signInfo']=1;self.job['allow_non_cancellable']=True
        self.assertEqual(r.check_candidate(self.detail,self.job,self.now),'eligible')


class GateTests(unittest.TestCase):
    def test_exact_write_one_shot_and_no_redirect_replay(self):
        b=r.RegistrationBrowser({});b.write_path='/login/wisdom-group-learning-bg/item/scItemRegistration/enter/synthetic'
        b.before_write=MagicMock();route=MagicMock()
        route.request.url=r.ORIGIN+b.write_path;route.request.method='POST'
        route.fetch.return_value.status=200;route.fetch.return_value.headers={};route.fetch.return_value.json.return_value={'success':True}
        with patch.object(r.network,'limiter'):
            b.guard(route);b.guard(route)
        self.assertEqual(route.fetch.call_count,1)
        self.assertEqual(b.before_write.call_count,1)
        self.assertEqual(route.fetch.call_args.kwargs['max_redirects'],0)
        self.assertEqual(route.fetch.call_args.kwargs['max_retries'],0)
        route.abort.assert_called_once()

    def test_foreign_and_unrelated_mutations_blocked(self):
        b=r.RegistrationBrowser({})
        for url in ['https://evil.invalid/item/scItemRegistration/enter/a',r.BASE+'/item/scItemFavorite/add']:
            route=MagicMock();route.request.url=url;route.request.method='POST';b.guard(route)
            route.abort.assert_called_once();route.fetch.assert_not_called()


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.jobs=r.Jobs('synthetic-account',Path(self.tmp.name))
        self.due=datetime.now(timezone.utc)+timedelta(minutes=1)
        self.job=self.jobs.create('合成测试活动',self.due.isoformat(),authorized=True)

    def execute(self, *, timeout=False, registered=True, early=False, extra=False):
        detail=activity(self.due)
        if extra:detail['needSignInfo']='1'
        browser=MagicMock()
        browser.open.return_value.__enter__.return_value=browser
        browser.locate.return_value=detail
        def submit(item_id, callback):
            callback()
            if timeout:raise TimeoutError('synthetic transport interruption')
            return {'success':True}
        browser.submit.side_effect=submit
        browser.detail.return_value={**detail,'booleanRegistration':1 if registered else 0}
        with patch.object(r,'Jobs',return_value=self.jobs),patch.object(r,'load_session',return_value={'account':'synthetic-account'}),patch.object(r,'RegistrationBrowser',return_value=browser),patch.object(r,'datetime',wraps=datetime) as clock:
            clock.now.return_value=self.due-timedelta(seconds=1) if early else self.due+timedelta(seconds=1)
            result=r.run_job(self.job['job_id'])
        return result,browser

    def test_success_is_independently_verified(self):
        result,browser=self.execute()
        self.assertEqual(result['state'],'verified');browser.detail.assert_called_once()
        self.assertFalse(result['result']['credit_hours_awarded'])

    def test_success_response_without_record_stays_uncertain(self):
        result,_=self.execute(registered=False)
        self.assertEqual(result['state'],'uncertain')

    def test_timeout_keeps_id_and_never_replays(self):
        result,_=self.execute(timeout=True)
        self.assertEqual(result['state'],'uncertain')
        self.assertEqual(result['result']['item_id'],'synthetic-event')
        result,browser=self.execute()
        browser.submit.assert_not_called()

    def test_early_run_never_opens_browser(self):
        result,browser=self.execute(early=True)
        self.assertEqual(result['state'],'scheduled');browser.open.assert_not_called()

    def test_unknown_required_fields_stop_before_write(self):
        result,browser=self.execute(extra=True)
        self.assertEqual(result['state'],'stopped');browser.submit.assert_not_called()


class BrowserContractTests(unittest.TestCase):
    def test_actual_headless_chrome_confirmation_controls(self):
        # All requests are fulfilled locally: no school request or real enrollment.
        html='''<div class="ant-modal"><button onclick="document.querySelector('.ant-popover').style.display='block'">报 名</button></div>
        <div class="ant-popover" style="display:none">确定报名吗?<button onclick="fetch('/login/wisdom-group-learning-bg/item/scItemRegistration/enter/synthetic',{method:'POST',body:'{}'})">确 定</button></div>'''
        b=r.RegistrationBrowser({});calls=[]
        with r.sync_playwright() as pw,r.chrome_browser(pw,headless=True) as chrome:
            context=chrome.new_context(service_workers='block')
            def fixture(route):
                if route.request.method=='GET' and route.request.url==r.ORIGIN+'/synthetic':
                    route.fulfill(status=200,content_type='text/html; charset=utf-8',body=html)
                elif route.request.method=='POST' and route.request.url==r.ORIGIN+b.write_path:
                    b.before_write();b.write_started=True;b.write_response={'success':True}
                    calls.append(route.request.url)
                    route.fulfill(status=200,content_type='application/json',body='{"success":true}')
                else:route.abort()
            context.route('**/*',fixture)
            b.page=context.new_page();b.page.goto(r.ORIGIN+'/synthetic')
            callback=MagicMock()
            result=b.submit('synthetic',callback)
            self.assertTrue(result['success']);self.assertEqual(len(calls),1);callback.assert_called_once()


if __name__=='__main__':unittest.main()
