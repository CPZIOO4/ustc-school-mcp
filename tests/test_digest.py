import copy
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock
import httpx

from school_mcp.digest import public,engine,calendar
from school_mcp.digest.store import Store,defaults

NOW=datetime(2026,10,12,0,0,tzinfo=timezone.utc)

class DigestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'SCHOOL_MCP_LOCAL_DIR':str(self.root)});self.env.start()
        self.owner=patch('school_mcp.digest.store.owner',return_value='synthetic-owner');self.owner.start()
        self.store=Store(self.root)
    def tearDown(self):self.owner.stop();self.env.stop();self.temp.cleanup()
    def config(self,**kw):
        return engine.configure(defaults()|dict(sources=['jobs'],enabled=True,urgent_hours=0,max_pages=1,detail_limit=0)|kw,True,self.store)
    def row(self,key='1',**kw):return public.item('jobs',key,'合成岗位','https://www.job.ustc.edu.cn/Jobinformation/list.aspx?itemid='+key,**kw)
    def collect(self,rows,**extra):return lambda *_:dict(items=copy.deepcopy(rows),complete=True,**extra)
    def cycle(self,rows,day=0,notify=True,**kw):return engine.refresh(store=self.store,now=NOW+timedelta(days=day),collector=self.collect(rows),notify=notify,**kw)
    def test_baseline_delta_and_restart_dedup(self):
        self.config();self.cycle([self.row()]);self.assertEqual(self.store.read()['messages'],[])
        self.cycle([self.row(),self.row('2')],1)
        s=self.store.read();self.assertEqual(len(s['messages']),1);self.assertEqual(len(s['messages'][0]['items']),1)
        self.cycle([self.row(),self.row('2')],2);self.assertEqual(len(Store(self.root).read()['messages']),1)
    def test_read_preview_preserves_pending_without_sending(self):
        self.config();self.cycle([self.row()]);self.cycle([self.row('2')],1,notify=False)
        self.assertEqual(len(self.store.read()['pending']),1);self.assertFalse(self.store.read()['messages'])
        self.cycle([self.row('2')],2);self.assertEqual(len(self.store.read()['messages']),1)
    def test_changed_deadline_and_unknown_not_inferred(self):
        self.config();self.cycle([self.row()]);x=self.row(deadline='2026-10-15')
        self.cycle([x],1);s=self.store.read();self.assertEqual(s['messages'][0]['items'][0]['change'],'changed')
        self.assertEqual(s['items'][x['id']]['revision'],1)
    def test_urgent_once_before_daily_and_not_stale(self):
        self.config(urgent_hours=24,daily_time='23:00');x=self.row(deadline='2026-10-12T09:00:00+08:00')
        self.cycle([x]);self.assertEqual(len(self.store.read()['messages']),1)
        engine.refresh(store=self.store,now=NOW+timedelta(hours=1),collector=self.collect([x]),notify=True)
        self.assertEqual(len(self.store.read()['messages']),1)
    def test_failure_is_visible_and_does_not_erase(self):
        self.config();self.cycle([self.row()])
        def fail(*_):raise RuntimeError('SECRET-CANARY')
        engine.refresh(store=self.store,now=NOW+timedelta(days=1),collector=fail,notify=True)
        s=self.store.read();self.assertEqual(len(s['items']),1);self.assertEqual(s['sources']['jobs']['state'],'failed')
        self.assertNotIn('SECRET-CANARY',json.dumps(s));self.assertTrue(s['messages'])
    def test_partial_empty_not_full_success(self):
        self.config();engine.refresh(store=self.store,now=NOW,collector=lambda *_:dict(items=[],complete=False,scope='partial'),notify=True)
        self.assertFalse(self.store.read()['sources']['jobs']['complete'])
    def test_disable_no_notify(self):
        self.config();self.cycle([self.row()]);self.config(enabled=False);self.cycle([self.row('2')],1)
        self.assertFalse(self.store.read()['messages'])
    def test_keyword_exclude_location(self):
        cfg=defaults()|dict(keywords=['AI'],exclude=['销售'],locations=['合肥'])
        self.assertEqual(engine.matches(self.row(summary='AI',location='合肥'),cfg),['AI'])
        self.assertFalse(engine.matches(self.row(summary='AI 销售',location='合肥'),cfg))
        self.assertFalse(engine.matches(self.row(summary='AI'),cfg))
    def test_encrypted_and_identity_bound(self):
        self.config();self.cycle([self.row(summary='PRIVATE-CANARY')])
        self.assertNotIn(b'PRIVATE-CANARY',self.store.path.read_bytes())
        with patch('school_mcp.digest.store.owner',return_value='different'):
            with self.assertRaises(ValueError):self.store.read()
    def test_concurrent_cycle_rejected(self):
        with self.store.cycle():
            with self.assertRaises(ValueError):
                with Store(self.root).cycle():pass
    def test_enable_requires_auth_and_invalid_settings(self):
        with self.assertRaises(ValueError):engine.configure(defaults()|{'enabled':True},False,self.store)
        for value in [{'sources':['bad']},{'interval_minutes':1},{'daily_time':'24:01'},{'email_enabled':True},{'sources':['bb']}]:
            with self.subTest(value=value),self.assertRaises(ValueError):engine.settings(value)
    def test_unknown_email_not_retried(self):
        mail=Mock(address='synthetic@example.test')
        draft={'draft_id':'synthetic','content_sha256':'abc'}
        with patch('school_mcp.mail.config.load_config',return_value=mail),patch('school_mcp.mail.outbox.prepare',return_value=draft) as prepare:
            self.config(email_enabled=True,email='recipient@example.test');self.cycle([self.row()])
            send=Mock(side_effect=TimeoutError('SMTP-CANARY'))
            self.cycle([self.row('2')],1,sender=send);self.cycle([self.row('2')],2,sender=send)
            self.assertEqual(send.call_count,1);self.assertEqual(prepare.call_count,1)
            self.assertEqual(self.store.read()['messages'][0]['delivery']['status'],'unknown')
    def test_mark_read(self):
        self.config();self.cycle([self.row()]);self.cycle([self.row('2')],1)
        message=self.store.read()['messages'][0];engine.mark_read(message['id'],self.store)
        self.assertTrue(self.store.read()['messages'][0]['read'])
    def test_pause_works_when_mail_binding_unavailable(self):
        self.config()
        with patch('school_mcp.mail.config.load_config',side_effect=ValueError('missing')):
            self.config(enabled=False,email_enabled=True,email='recipient@example.test')
        self.assertFalse(self.store.read()['settings']['enabled'])
    def test_recover_only_original_ready_draft(self):
        mail=Mock(address='synthetic@example.test');send=Mock(return_value={'status':'accepted'})
        with patch('school_mcp.mail.config.load_config',return_value=mail):
            self.config(email_enabled=True,email='recipient@example.test')
            state=self.store.read()
            for status in ['ready','unknown','accepted','sending']:
                message={'text':'test','subscription_version':state['subscription_version'],'delivery':{'status':'sending','draft_id':'original'}}
                with patch('school_mcp.mail.outbox.status',return_value={'status':status,'draft_id':'original','content_sha256':'hash'}),patch('school_mcp.mail.outbox.prepare') as prepare:
                    engine._deliver(state,message,self.store,send);prepare.assert_not_called()
            send.assert_called_once_with(mail,'original','hash')
    def test_calendar_versions_survive_export_and_change(self):
        event=dict(id='synthetic',title='课程',start='2026-10-12',location='A')
        a=Path(calendar.export([event])['path']).read_text(encoding='utf-8')
        b=Path(calendar.export([event|{'location':'B'}])['path']).read_text(encoding='utf-8')
        c=Path(calendar.export([event|{'location':'B'}])['path']).read_text(encoding='utf-8')
        self.assertIn('SEQUENCE:0',a);self.assertIn('SEQUENCE:1',b);self.assertIn('SEQUENCE:1',c)
        self.assertEqual(next(s for s in a.splitlines() if s.startswith('UID:')),next(s for s in b.splitlines() if s.startswith('UID:')))
    def test_pending_delivery_recovers_after_crash_before_prepare(self):
        mail=Mock(address='synthetic@example.test');send=Mock(return_value={'status':'accepted'})
        with patch('school_mcp.mail.config.load_config',return_value=mail),patch('school_mcp.mail.outbox.prepare',return_value={'draft_id':'original','content_sha256':'hash'}):
            self.config(email_enabled=True,email='recipient@example.test')
            state=self.store.read();state['messages']=[{'id':'x','text':'test','subscription_version':state['subscription_version'],'delivery':{'status':'pending'}}];self.store.save(state)
            self.cycle([],sender=send)
            send.assert_called_once_with(mail,'original','hash')
    def test_large_report_is_bounded_and_disclosed(self):
        rows=[self.row(str(i),summary='中'*400,location='中'*180) for i in range(100)]
        report=engine.report_text(rows,{})
        self.assertLess(len(report),16000);self.assertIn('共 100 项',report)
    def test_model_only_selects_real_evidence(self):
        from school_mcp.digest.summary import summarize
        model=Mock();model.complete.return_value={'content':'{"priority_ids":["invented"]}'}
        result=summarize(dict(items=[self.row()],source_health={}),model)
        self.assertEqual(result['mode'],'deterministic_fallback');self.assertNotIn('invented',result['text'])

class CalendarTests(unittest.TestCase):
    def test_folding_escaping_stable_uid_and_unknown_skip(self):
        events=[dict(id='x',title='课程中文'*40+'\nBEGIN:VEVENT',start='2026-10-12T10:00:00+08:00',end='2026-10-12T11:00:00+08:00'),dict(id='no',title='unknown')]
        a=calendar.render(events,NOW);b=calendar.render(events,NOW+timedelta(days=1))
        self.assertEqual(a['count'],1);self.assertEqual(len(a['skipped']),1)
        self.assertTrue(all(len(line.encode())<=75 for line in a['content'].split('\r\n')))
        self.assertIn('DTSTART:20261012T020000Z',a['content'])
        self.assertEqual([l for l in a['content'].splitlines() if l.startswith('UID:')],[l for l in b['content'].splitlines() if l.startswith('UID:')])
        self.assertEqual(a['content'].count('\r\nBEGIN:VEVENT\r\n'),1)
    def test_all_day_and_invalid_end(self):
        r=calendar.render([dict(id='1',title='due',deadline='2026-10-12'),dict(id='2',start='2026-10-12',end='2026-10-11')],NOW)
        self.assertIn('DTEND;VALUE=DATE:20261013',r['content']);self.assertEqual(r['count'],1)
    def test_official_dates_only_no_weekday_arithmetic(self):
        c=Mock();c.course_data.return_value=({'semester':{'id':1}},{'oddWeekIndex2dayOfWeek2Date':{'1':{'1':'2026-10-13'}}})
        c.timetable.return_value={'activities':[dict(lesson_id=1,weekday=1,weeks=[1,2],start='08:00',end='09:40',start_period=1,end_period=2)]}
        r=calendar.timetable(client=c);self.assertEqual(len(r['events']),1);self.assertEqual(len(r['skipped']),1)
        self.assertTrue(r['events'][0]['start'].startswith('2026-10-13'))

class PublicTests(unittest.TestCase):
    def test_no_date_inference(self):
        incomplete=public._fields('报名截止：10月15日')
        self.assertNotIn('deadline',incomplete);self.assertIn('缺少年份',incomplete['time_warning'])
        r=public._fields('报告时间：2026年10月12日 10:00-11:00\n报告地点：合成楼')
        self.assertEqual(r['start'],'2026-10-12T10:00:00+08:00');self.assertIn('end',r)
    def test_url_restriction(self):
        for u in ['https://evil.test/2026/1012/c1a2/page.htm','https://math.ustc.edu.cn/admin?delete=1','http://math.ustc.edu.cn/2026/1012/c1a2/page.htm','https://math.ustc.edu.cn/2026/1012/c1a2/page.htm?token=x']:
            with self.subTest(url=u),self.assertRaises(ValueError):public.detail('math_notices',u)
    def test_ambiguous_and_contradictory_dates_stay_unknown(self):
        for raw in ['2026年10月12日 至 2026年10月14日','2026年10月12日（周日）10:00']:
            self.assertNotIn('start',public._fields('报告时间：'+raw))
    def test_only_actual_list_no_nav(self):
        html='<nav><li><a href="/2026/1001/c1a1/page.htm">导航</a></li></nav><div id="wp_news_w07"><blockquote class="hero-unify"><h4><a href="/2026/1001/c2a2/page.htm" title="真实报告">报告</a></h4></blockquote></div>'
        r=public.parse_college('physics_lectures',html,'https://physics.ustc.edu.cn/xsbg/list.htm',1)
        self.assertEqual([x['title'] for x in r['items']],['真实报告'])
    def test_structure_change_not_empty(self):
        with self.assertRaises(Exception):public.parse_college('math_notices','<html>login</html>','https://math.ustc.edu.cn/tzgg/list.htm',1)

if __name__=='__main__':unittest.main()
