"""Scholarship boundaries and recovery. Synthetic identities, no school writes."""
from contextlib import nullcontext
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from school_mcp.student import applications as a
from school_mcp.workflow_store import PlanStore, WorkflowError
from school_mcp.service_errors import ServiceError
from school_mcp.agent import engine
from school_mcp.agent.store import Store
from school_mcp.agent.catalog import entry_schema, describe


def values():
    return {'nation':'汉族','political_status':'群众','entry_month':'2023-09',
            'email':'applicant@example.test','campus_address':'合成宿舍','phone':'10000000000',
            'school_zip':'000000','school_name':'合成高级','statement':'仅供自动测试使用的合成材料，不对应任何真人经历。'*8}


TARGET = dict(name=a.TITLE,year='2030',starts='2000-01-01',ends='2099-12-31',
              url='https://xgyth.ustc.edu.cn/jxj/scholarship/pbApplyTable.aspx?itemID=1001')


class Rules(unittest.TestCase):
    def test_urls_cannot_carry_foreign_host_or_other_actions(self):
        self.assertEqual(a.form_url('pbApplyTable.aspx?itemID=1001'),TARGET['url'])
        for url in ['https://evil.test'+a.FORM+'?itemID=1001', 'http://xgyth.ustc.edu.cn'+a.FORM+'?itemID=1001',
                    TARGET['url']+'&action=delete', TARGET['url']+'&itemID=1002', TARGET['url']+'#token',
                    'pbApplyTableSave.aspx?funID=4', 'pbApplyTable.aspx?itemID=0',
                    'https://xgyth.ustc.edu.cn:444'+a.FORM+'?itemID=1001']:
            with self.subTest(url=url), self.assertRaises(ServiceError): a.form_url(url)

    def test_missing_fields_and_short_statement_return_together(self):
        issues=a.validate({'statement':'只提供一句话'},[])
        self.assertIn('/fields/nation',[v['path'] for v in issues])
        self.assertIn('/fields/statement',[v['path'] for v in issues])
        self.assertIn('/fields/campus_address',[v['path'] for v in a.validate(values()|{'campus_address':'  '},[])])
        self.assertFalse(a.validate(values(),['美国大学生数模竞赛','志愿服务']))
        with self.assertRaises(ServiceError):a.validate(values()|{'award_amount':'1000'},[])
        with self.assertRaises(ServiceError):a.validate(values(),['不存在的竞赛'])
        with self.assertRaises(ServiceError):a.validate(values(),['志愿服务','志愿服务'])
        self.assertTrue(a.validate(values()|{'entry_month':'2023-19','email':'bad','phone':'abc'},[]))

    def test_mutation_guard_never_allows_deletion_or_duplicate_submit(self):
        b=object.__new__(a.BrowserApplication);b.mode='fill';b.sent=0;b.blocked=False
        def request(path,body=''):
            route=Mock();route.request.url='https://'+a.HOST+path;route.request.method='POST';route.request.post_data=body
            b.guard(route);return route
        self.assertTrue(request(a.SAVE,'funID=2').fallback.called)
        self.assertTrue(request(a.SAVE,'funID=4').abort.called)
        b.mode='submit'
        self.assertTrue(request(a.SAVE,'funID=4').fallback.called)
        self.assertTrue(request(a.SAVE,'funID=4').abort.called)
        self.assertTrue(request(a.SAVE,'funID=3').abort.called)
        self.assertTrue(request('/jxj/unknown.aspx','funID=2').abort.called)
        self.assertTrue(request(a.SAVE,'funID=4&funID=2').abort.called)
        b.mode='read'
        self.assertTrue(request('/jxj/service/const.aspx','funID=2').fallback.called)


@unittest.skipUnless(os.name=='nt','DPAPI')
class ApplicationLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        env=patch.dict(os.environ,{'SCHOOL_MCP_LOCAL_DIR':self.tmp.name});env.start();self.addCleanup(env.stop)
        account=patch.object(a,'load_credentials',return_value={'username':'synthetic-account'});account.start();self.addCleanup(account.stop)
        self.ref=a.remember('synthetic-account',a.TITLE,
            ['2030','2000-01-01','2099-12-31','点击申请'],[('申请',TARGET['url'])])['application_reference']

    def plan(self,fields=None):
        return a.prepare(self.ref,fields or values(),['美国大学生数模竞赛','志愿服务'])

    def test_prepare_stays_local_and_binds_full_preview_without_inventing_values(self):
        with patch.object(a,'browser') as web:
            missing=a.prepare(self.ref,{'statement':'太短'})
            self.assertEqual(missing['state'],'needs_input');web.assert_not_called()
            plan=self.plan();web.assert_not_called()
        self.assertEqual(plan['preview']['fields']['cadre'],'')
        self.assertEqual(plan['preview']['requested_level'],'铜奖')
        self.assertIn('不代表等级',plan['preview']['note'])
        self.assertEqual(self.plan()['plan_id'],plan['plan_id'])
        data=(Path(self.tmp.name)/'business-plans.sqlite3').read_bytes()
        self.assertNotIn(b'applicant@example.test',data)

    def test_reference_and_plan_are_bound_to_current_identity(self):
        plan=self.plan()
        with patch.object(a,'load_credentials',return_value={'username':'another-synthetic'}):
            with self.assertRaises(ServiceError):a.reference(self.ref)
            with self.assertRaises(WorkflowError):a.status(plan['plan_id'],False)

    def test_execute_checks_authorization_and_digest_before_browser(self):
        plan=self.plan()
        with patch.object(a,'browser') as web:
            with self.assertRaises(ServiceError):a.submit(plan['plan_id'],plan['content_sha256'],False)
            with self.assertRaises(ServiceError):a.submit(plan['plan_id'],'0'*64,True)
            web.assert_not_called()

    def test_already_submitted_never_opens_mutating_form(self):
        plan=self.plan();web=Mock();web.lookup.return_value=TARGET|{'status':'个人已提交'}
        with patch.object(a,'browser',return_value=nullcontext(web)):
            result=a.submit(plan['plan_id'],plan['content_sha256'],True)
        self.assertEqual(result['state'],'already_submitted');web.open.assert_not_called();web.submit.assert_not_called()

    def test_save_readback_failure_never_reaches_submit(self):
        plan=self.plan();web=Mock();web.lookup.return_value=TARGET|{'status':'个人未提交'};web.fill.side_effect=ValueError('mismatch')
        with patch.object(a,'browser',return_value=nullcontext(web)):
            result=a.submit(plan['plan_id'],plan['content_sha256'],True)
        self.assertEqual(result['state'],'uncertain');web.submit.assert_not_called()

    def test_interrupted_submit_is_not_replayed_and_only_original_status_unlocks(self):
        plan=self.plan();web=Mock();web.lookup.return_value=TARGET|{'status':'个人未提交'};web.submit.side_effect=TimeoutError()
        with patch.object(a,'browser',return_value=nullcontext(web)):
            self.assertEqual(a.submit(plan['plan_id'],plan['content_sha256'],True)['state'],'uncertain')
            self.assertEqual(a.submit(plan['plan_id'],plan['content_sha256'],True)['state'],'uncertain')
            self.assertEqual(a.status(plan['plan_id'])['state'],'uncertain')
            newer=self.plan(values()|{'cadre':'合成修订'})
            with self.assertRaises(WorkflowError):a.submit(newer['plan_id'],newer['content_sha256'],True)
            web.submit.assert_called_once()
            web.lookup.return_value=TARGET|{'status':'个人已提交'}
            result=a.status(plan['plan_id'])
        self.assertEqual(result['state'],'verified');self.assertFalse(result['result']['award_level_confirmed'])

    def test_confirmed_validation_failure_does_not_become_success(self):
        plan=self.plan();web=Mock();web.lookup.return_value=TARGET|{'status':'个人未提交'}
        web.submit.return_value={'validation_rejected':True,'success_alert':False,'request_count':1}
        with patch.object(a,'browser',return_value=nullcontext(web)):
            result=a.submit(plan['plan_id'],plan['content_sha256'],True)
        self.assertEqual(result['state'],'stopped');self.assertFalse(result['result']['submitted'])

    def test_success_requires_independent_official_list(self):
        plan=self.plan();web=Mock();web.lookup.side_effect=[TARGET|{'status':'个人未提交'},TARGET|{'status':'个人已提交'}]
        web.submit.return_value={'validation_rejected':False,'success_alert':True,'request_count':1}
        with patch.object(a,'browser',return_value=nullcontext(web)):
            result=a.submit(plan['plan_id'],plan['content_sha256'],True)
        self.assertEqual(result['state'],'verified');web.open.assert_called_once();web.fill.assert_called_once()
        self.assertEqual(web.lookup.call_count,2)

    def test_coordinator_enforces_permission_and_provides_status_link(self):
        task=Store().create('student','synthetic scholarship request',None)['task_id']
        args={'plan_id':'a'*32,'content_sha256':'b'*64}
        with patch.object(engine,'identity',return_value=None),patch.object(a,'submit',return_value={'state':'uncertain','plan_id':'a'*32}) as submit:
            denied=asyncio.run(engine.call(task,'school_student_submit_application',args,False))
            self.assertFalse(denied['dispatched']);submit.assert_not_called()
            result=asyncio.run(engine.call(task,'school_student_submit_application',args,True))
            self.assertEqual(result['state'],'needs_verification')
            self.assertTrue(submit.call_args.args[2])
            self.assertEqual(result['links'][0]['tool'],'school_student_application_status')
        with patch.object(engine,'identity',return_value=None),patch.object(a,'status',return_value={'state':'verified','plan_id':'a'*32}):
            checked=asyncio.run(engine.call(task,'school_student_application_status',{'plan_id':'a'*32}))
            self.assertEqual(checked['state'],'verified')

    def test_local_preparation_failure_does_not_block_task_as_remote_write(self):
        task=Store().create('student','synthetic',None)['task_id']
        with patch.object(engine,'identity',return_value=None),patch.object(a,'prepare',side_effect=ValueError('invalid')):
            asyncio.run(engine.call(task,'school_student_prepare_application',{'application_reference':self.ref,'fields':{}}))
        self.assertFalse(Store().read(task)['write_blocked'])
