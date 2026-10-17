import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
import httpx
from bs4 import BeautifulSoup
from school_mcp.digest import public,planner,engine,posters
from school_mcp.digest.store import Store,defaults

class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.env=patch.dict(os.environ,{'SCHOOL_MCP_LOCAL_DIR':self.tmp.name});self.env.start();self.addCleanup(self.env.stop)
    def model(self,changes):
        return Mock(complete=Mock(return_value={'content':json.dumps({'changes':changes},ensure_ascii=False)}))
    def test_desktop_worker_survives_denied_breakaway_and_holds_real_lock(self):
        import threading,time
        from school_mcp.digest import worker
        state=Store().read();state['settings']['enabled']=True;Store().save(state)
        entered=threading.Event();release=threading.Event()
        def collect(**kwargs):
            entered.set();release.wait(5)
        with patch('school_mcp.background_process.spawn',side_effect=PermissionError('denied')),patch('school_mcp.digest.engine.refresh',side_effect=collect):
            result=worker.ensure(allow_in_process=True)
            try:
                self.assertTrue(entered.wait(2));self.assertTrue(worker.running())
                self.assertEqual(result['mode'],'application_process')
                self.assertTrue(worker.ensure(allow_in_process=True)['running'])
            finally:
                state=Store().read();state['settings']['enabled']=False;Store().save(state);release.set()
                until=time.monotonic()+7
                while worker.running() and time.monotonic()<until:time.sleep(.05)
            self.assertFalse(worker.running())
    def test_short_lived_worker_caller_never_silently_falls_back(self):
        from school_mcp.digest import worker
        state=Store().read();state['settings']['enabled']=True;Store().save(state)
        # Isolate worker-launch policy; its Thread mock must not intercept native storage pipe IO.
        with patch.object(Store,'read',return_value=state),patch('school_mcp.digest.native.check_paths'),patch('school_mcp.background_process.spawn',side_effect=PermissionError('denied')),patch('threading.Thread') as thread:
            with self.assertRaises(PermissionError):worker.ensure()
            thread.assert_not_called()
    def test_presets_do_not_enable_or_change_storage(self):
        r=planner.propose(preset='lectures_careers',base=defaults()|{'enabled':True,'daily_time':'10:00'})
        self.assertFalse(r['settings']['enabled']);self.assertEqual(r['settings']['daily_time'],'10:00')
        self.assertEqual(len(r['settings']['sources']),7);self.assertFalse(Store().read()['settings']['enabled'])
    def test_sentence_can_only_propose_backed_fields(self):
        request='实习招聘，每天早上10点，只在应用内提醒'
        model=self.model({'sources':{'value':['jobs','internships'],'evidence':'实习招聘'},'daily_time':{'value':'10:00','evidence':'早上10点'}})
        r=planner.propose(request,base=defaults()|{'email_enabled':True,'email':'synthetic@example.test'},model=model)
        self.assertEqual(r['settings']['daily_time'],'10:00');self.assertFalse(r['settings']['email_enabled']);self.assertFalse(r['saved'])
    def test_bad_model_changes_fail_without_mutation(self):
        for change in [ {'enabled':{'value':True,'evidence':'开启'}}, {'sources':{'value':['unknown'],'evidence':'开启'}},
                        {'keywords':{'value':[' invented '],'evidence':'开启'}},{'daily_time':{'value':'10:00','evidence':'非用户原话'}}]:
            with self.subTest(change=change),self.assertRaises(ValueError):planner.propose('开启',model=self.model(change))
        self.assertFalse(Store().read()['settings']['enabled'])
    def test_course_preset_reports_missing_course(self):
        r=planner.propose(preset='coursework');self.assertEqual(r['state'],'needs_input');self.assertTrue(r['missing'])
    def test_preview_inbox_never_sends_or_consumes_changes(self):
        s=Store();state=s.read();state['settings'].update(email_enabled=True,email='synthetic@example.test');state['pending']={'test':{'id':'test'}};s.save(state)
        with patch('school_mcp.mail.outbox.send') as send,patch('school_mcp.mail.outbox.prepare') as prepare:
            a=engine.preview_report(s);b=engine.preview_report(s);send.assert_not_called();prepare.assert_not_called()
        self.assertEqual(a['message_id'],b['message_id']);self.assertEqual(a['delivery']['status'],'app_only')
        self.assertEqual(s.read()['pending'],state['pending']);self.assertFalse(s.read()['messages'][0]['read'])
    def test_college_redirect_follows_only_fixed_same_origin_pages(self):
        seen=[]
        def handle(request):
            seen.append(str(request.url))
            if request.url.path=='/_redirect':return httpx.Response(302,headers={'location':'http://physics.ustc.edu.cn/2026/1001/c1a2/page.htm'})
            return httpx.Response(200,text='article')
        r,u=public.college_response('https://physics.ustc.edu.cn/_redirect?siteId=63&columnId=1&articleId=2','physics.ustc.edu.cn',httpx.MockTransport(handle))
        self.assertEqual(r.text,'article');self.assertTrue(u.startswith('https://'));self.assertEqual(len(seen),2)
        with self.assertRaises(ValueError):
            public.college_response('https://physics.ustc.edu.cn/2026/1001/c1a2/page.htm','physics.ustc.edu.cn',httpx.MockTransport(lambda r:httpx.Response(302,headers={'location':'https://evil.test/a'})))
    def test_headline_not_overwritten_by_description_link(self):
        html='<div id="wp_news_w07"><div class="hero-unify"><a href="/_redirect?siteId=63&columnId=1&articleId=2">标题</a><a href="/_redirect?siteId=63&columnId=1&articleId=2">长摘要</a></div></div>'
        r=public.parse_college('physics_lectures',html,'https://physics.ustc.edu.cn/xsbg/list.htm',1)
        self.assertEqual(r['items'][0]['title'],'标题');self.assertEqual(r['unsupported_links'],0)
    def test_ocr_external_image_not_requested_and_cache_reused(self):
        body=BeautifulSoup('<img src="https://evil.test/x.png"><img src="/_upload/article/images/a.png">','html.parser')
        get=Mock(return_value=httpx.Response(200,content=b'synthetic-image',headers={'content-type':'image/png'}))
        with patch('school_mcp.desktop.document_io.image_text',return_value={'content':'时间：2026年10月15日 10:00','minimum_confidence':.98}) as ocr:
            a=posters.read_images(body,'https://cs.ustc.edu.cn/2026/1001/c1a2/page.htm',get)
            b=posters.read_images(body,'https://cs.ustc.edu.cn/2026/1001/c1a2/page.htm',get)
            self.assertEqual(ocr.call_count,1)
        self.assertEqual(a['skipped'],1);self.assertEqual(len(a['images']),1);self.assertTrue(a['images'][0]['requires_review'])
        self.assertTrue(all(c.args[0].startswith('https://cs.ustc.edu.cn/_upload/') for c in get.call_args_list))
    def test_ocr_dates_are_candidates_not_calendar_times(self):
        html='<h1>合成海报讲座</h1><div class="wp_articlecontent"><img src="/_upload/article/images/a.png"></div>'
        transport=httpx.MockTransport(lambda r:httpx.Response(200,text=html))
        with patch('school_mcp.digest.posters.read_images',return_value={'images':[{'text':'报告时间：2026年10月15日 10:00','source_url':'image','requires_review':True}],'skipped':0,'note':'OCR需核对'}):
            result=public.detail('cs_lectures','https://cs.ustc.edu.cn/2026/1001/c1a2/page.htm',transport)
        self.assertIsNone(result['start']);self.assertEqual(result['time_candidates']['start'],'2026-10-15T10:00:00+08:00')
    def test_invalid_clock_not_silently_all_day(self):
        self.assertNotIn('start',public._fields('报告时间：2026年10月15日 25:10'))

if __name__=='__main__':unittest.main()
