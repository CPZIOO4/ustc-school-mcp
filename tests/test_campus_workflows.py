"""Critical bounds and no-replay paths using synthetic accounts and receipts."""
from contextlib import nullcontext
from datetime import datetime, timedelta
import hashlib
import asyncio
import threading
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import httpx

from school_mcp.campus.session import checked_url, scoped
from school_mcp.campus import session
from school_mcp.drive.client import drive_path, signature, transfer_url
from school_mcp.drive import operations
from school_mcp.spaces import reservations
from school_mcp.vlab import jobs
from school_mcp.web.client import safe_url
from school_mcp.web import client as public_web
from school_mcp.service_errors import ServiceError
from school_mcp.workflow_store import PlanStore


class AsyncBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_dispatch_runs_browser_work_outside_async_loop(self):
        from school_mcp.agent.catalog import adapter, WORKFLOWS
        from school_mcp.portal import client
        original = threading.get_ident()
        def query(*args):
            self.assertNotEqual(threading.get_ident(),original)
            with self.assertRaises(RuntimeError):asyncio.get_running_loop()
            return {'items':[], 'total':0}
        with patch.object(client,'progress',side_effect=query):
            result=await adapter('portal').call_tool('school_portal_progress',{})
        self.assertEqual(result[1]['total'],0)
        for site in ('portal','student','spaces','drive','vlab','web'):
            for name in WORKFLOWS[site]['tools']:
                tool=adapter(site)._tool_manager.get_tool(name)
                self.assertTrue(tool.is_async,name)
                self.assertIsNotNone(tool.output_schema,name)


class Boundaries(unittest.TestCase):
    def test_fresh_shared_identity_overrides_old_site_snapshot(self):
        cookie={'name':'synthetic-session','domain':'id.ustc.edu.cn','path':'/'}
        own={'cookies':[cookie|{'value':'synthetic-old'},{'name':'site','domain':'pan.ustc.edu.cn','path':'/','value':'synthetic-site'}],'origins':[]}
        shared={'cookies':[cookie|{'value':'synthetic-new'}],'origins':[]}
        with patch.object(session,'load_device_state',return_value={'state':shared}), \
             patch.object(session,'read_private',return_value={'account':'synthetic-account','state':own}):
            value=session.initial_state('drive','synthetic-account')
        self.assertEqual({c['value'] for c in value['cookies']},{'synthetic-new','synthetic-site'})

    def test_url_and_session_scopes(self):
        for url in ('http://pan.ustc.edu.cn/a', 'https://pan.ustc.edu.cn.evil.test/a',
                    'https://user:password@pan.ustc.edu.cn/a', 'https://pan.ustc.edu.cn:444/a', 'data:text/plain,secret'):
            with self.assertRaises(ServiceError): transfer_url(url)
            with self.assertRaises(ServiceError): checked_url('drive', url)
        state=scoped('drive', {'cookies':[{'name':'session','value':'synthetic','domain':'.pan.ustc.edu.cn'},{'domain':'other.test'}],
                              'origins':[{'origin':'https://pan.ustc.edu.cn'},{'origin':'https://other.test'}]})
        self.assertEqual(len(state['cookies']),1)
        self.assertEqual(len(state['origins']),1)
        for path in ('/a/../b','relative','/a\\b','/a\x00b'):
            with self.assertRaises(ServiceError):drive_path(path)

    def test_public_reader_never_opens_business_or_auth_urls(self):
        for url in ('https://vlab.ustc.edu.cn/vm/', 'https://ustc.life/?token=example',
                    'https://www.teach.ustc.edu.cn/../admin', 'https://llm.ustc.edu.cn/guide/%2e%2e/secrets'):
            with self.assertRaises(ServiceError): safe_url(url)
        self.assertEqual(safe_url('https://ustc.life/#a'),'https://ustc.life/')
        self.assertEqual(safe_url('https://www.teach.ustc.edu.cn/?attachment_id=123',attachment=True),
                         'https://www.teach.ustc.edu.cn/?attachment_id=123')
        for suffix in ('attachment_id=123&action=delete','action=delete','attachment_id=0','attachment_id=123&token=example'):
            with self.assertRaises(ServiceError):safe_url('https://www.teach.ustc.edu.cn/?'+suffix,attachment=True)

    def test_drive_signature_nested_params(self):
        result=signature({'src_dirents':['a.txt','b.txt'],'empty':'','active':False}, 'synthetic-client', timestamp=123)
        literal='active=0&client_id=synthetic-client&src_dirents[0]=a.txt&src_dirents[1]=b.txt&timestamp=123'
        self.assertEqual(result['sn'],hashlib.md5(literal.encode()).hexdigest())

    def test_remote_download_cannot_escape_vlab_job(self):
        sftp=Mock();sftp.normalize.side_effect=lambda p:'/etc/passwd' if p.endswith('/link') else p
        for path in ('../secret','/etc/passwd','a\\b','link'):
            with self.assertRaises(ServiceError):jobs.remote_path(sftp,'/home/test/job',path)
        self.assertEqual(jobs.remote_path(sftp,'/home/test/job','result.txt'),'/home/test/job/result.txt')

    def test_reservation_conflict_people_rules_and_timezone(self):
        now=datetime(2030,1,1,8,tzinfo=reservations.CST)
        begin=now+timedelta(hours=2);end=begin+timedelta(hours=1)
        room={'devStatus':0,'minUser':1,'maxUser':3,'openTimes':[{'openStartTime':'08:00','openEndTime':'22:00'}],
              'resvRule':{'minResvTime':30,'maxResvTime':120,'timeInterval':30}}
        reservations.validate_room(room,begin,end,1,now)
        for changed, people in [({'onlyView':True},1),({},4),({'resvInfo':[{'startTime':'10:30','endTime':'11:30'}]},1),
                                ({'resvInfo':[{'unknown':'format'}]},1)]:
            with self.assertRaises(ServiceError):reservations.validate_room(room|changed,begin,end,people,now)
        self.assertEqual(reservations.timestamp('2030-01-01T02:00:00+00:00'),begin)
        with patch.object(reservations,'browser_session') as browser:
            with self.assertRaises(ServiceError):reservations.prepare_cancel('not-date','a'*32)
            browser.assert_not_called()


@unittest.skipUnless(os.name=='nt','DPAPI')
class WriteLifecycle(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        env=patch.dict(os.environ,{'SCHOOL_MCP_LOCAL_DIR':tmp.name});env.start();self.addCleanup(env.stop)

    def test_public_attachment_resolves_only_unique_official_file(self):
        ref='a'*24;url='https://www.teach.ustc.edu.cn/?attachment_id=123'
        page='https://www.teach.ustc.edu.cn/notice/notice-info/123.html/attachment/synthetic'
        binary='https://www.teach.ustc.edu.cn/wp-content/uploads/synthetic.pdf'
        calls=[]
        def response(client, method, target, **kwargs):
            calls.append(target)
            if target==url:return httpx.Response(301,headers={'location':page})
            if target==page:return httpx.Response(200,headers={'content-type':'text/html'},text='<main><article class="attachment"><a href="'+binary+'">Download</a><a href="https://external.invalid/leak.pdf">Outside</a></article></main>')
            if target==binary:return httpx.Response(200,headers={'content-type':'application/pdf'},content=b'%PDF-synthetic')
            self.fail('unexpected URL')
        with patch.object(public_web,'read_private',return_value={ref:url}),patch.object(public_web,'http_client',return_value=nullcontext(Mock())), \
             patch.object(public_web,'bounded_request',side_effect=response):
            result=public_web.download(ref)
        self.assertEqual(calls,[url,page,binary]);self.assertEqual(Path(result['path']).read_bytes(),b'%PDF-synthetic')

    def test_attachment_html_or_cross_host_redirect_never_becomes_file(self):
        ref='a'*24;url='https://www.teach.ustc.edu.cn/?attachment_id=123'
        responses=[httpx.Response(301,headers={'location':'https://finance.ustc.edu.cn/test.pdf'}),
                   httpx.Response(200,headers={'content-type':'text/html'},text='<main><article class="attachment"><a href="/a.pdf">A</a><a href="/b.pdf">B</a></article></main>')]
        for response in responses:
            with patch.object(public_web,'read_private',return_value={ref:url}),patch.object(public_web,'http_client',return_value=nullcontext(Mock())), \
                 patch.object(public_web,'bounded_request',return_value=response):
                with self.assertRaises(ServiceError):public_web.download(ref)

    def test_drive_lost_response_never_resends_then_readback_verifies(self):
        saved=PlanStore('drive','synthetic-account')
        c={'operation':'mkdir','repo_id':'a'*36,'path':'/','name':'synthetic-folder','destination':'','files':[]}
        p=saved.create('synthetic-target',c,{}, {},c)
        drive=Mock();drive.listing.return_value=[];drive.call.side_effect=TimeoutError()
        with patch.object(operations,'store',return_value=saved),patch.object(operations,'connected',side_effect=lambda:nullcontext(drive)):
            self.assertEqual(operations.execute(p['plan_id'],p['content_sha256'],True)['state'],'uncertain')
            self.assertEqual(operations.execute(p['plan_id'],p['content_sha256'],True)['state'],'uncertain')
            self.assertEqual(drive.call.call_count,1)
            drive.listing.return_value=[{'name':'synthetic-folder','type':'dir'}]
            self.assertEqual(operations.status(p['plan_id'])['state'],'verified')
            self.assertEqual(drive.call.call_count,1)

    def test_drive_collision_and_changed_material_stop_before_any_write(self):
        drive=Mock();drive.listing.return_value=[{'name':'existing.txt'}]
        item={'plan':{'content':{'operation':'mkdir','repo_id':'a'*36,'path':'/','name':'existing.txt'}}}
        with self.assertRaises(ServiceError):operations.validate_current(drive,item)
        drive.call.assert_not_called()
        saved=PlanStore('drive','synthetic-account');file=self.root/'input.txt';file.write_text('original')
        c={'operation':'upload','repo_id':'a'*36,'path':'/','name':'','destination':'',
           'files':[{'path':str(file),'name':file.name,'size':8,'sha256':hashlib.sha256(b'original').hexdigest()}]}
        p=saved.create('synthetic-upload',c,{}, {},{})
        file.write_text('modified')
        with patch.object(operations,'store',return_value=saved),patch.object(operations,'connected') as browser:
            with self.assertRaises(ServiceError):operations.execute(p['plan_id'],p['content_sha256'],True)
            browser.assert_not_called()
        self.assertEqual(saved.get(p['plan_id'])['state'],'ready')

    def test_captcha_stops_before_claim_and_reservation_post(self):
        saved=PlanStore('spaces','synthetic-account')
        c={'operation':'reserve','appAccNo':1,'kind_id':1,'dev_id':2,'begin':'2030-01-01 10:00',
           'end':'2030-01-01 11:00','members':[1],'title':'synthetic-purpose'}
        p=saved.create('synthetic-room',c,{}, {},{})
        with patch.object(reservations,'store',return_value=saved),patch.object(reservations,'browser_session',return_value=nullcontext(Mock())), \
             patch.object(reservations,'connected',return_value={'accNo':1}),patch.object(reservations,'room',return_value={}), \
             patch.object(reservations,'validate_room'),patch.object(reservations,'request',return_value={'data':[{'sysKey':'resvCode','sysValue':'1'}]}), \
             patch.object(reservations,'post') as post:
            value=reservations.execute(p['plan_id'],p['content_sha256'],True)
            self.assertEqual(value['state'],'manual_verification_required');post.assert_not_called()
        self.assertEqual(saved.get(p['plan_id'])['state'],'ready')

    def test_vlab_unknown_start_keeps_claim_and_never_reexecutes(self):
        saved=PlanStore('vlab','synthetic-account')
        p=saved.create('synthetic-vm',{'vm_id':1,'username':'vlab','command':'true','timeout':30,'files':[]},{}, {},{})
        with patch.object(jobs,'store',return_value=saved),patch.object(jobs,'ssh',side_effect=ConnectionError()) as ssh:
            self.assertEqual(jobs.execute(p['plan_id'],p['content_sha256'],True)['state'],'uncertain')
            self.assertEqual(jobs.execute(p['plan_id'],p['content_sha256'],True)['state'],'uncertain')
            self.assertEqual(ssh.call_count,1)
