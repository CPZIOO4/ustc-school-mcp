import inspect
import json
import unittest
from unittest.mock import Mock, patch

from school_mcp.finance.preflight import PreflightRequest, preflight
from school_mcp.nan7.publishing import Publisher
from school_mcp.script_workflows import prepare_bb_by_name, plan_timetable
from school_mcp.jw.planner import Constraints, generate
from test_business_workflows import candidate
from test_finance_preflight import sample


class InputGuidanceTests(unittest.TestCase):
    def check_guide(self,result,tool):
        guide=result['input_guidance']
        self.assertEqual(guide['resume']['tool'],tool)
        self.assertTrue(guide['fields'])
        self.assertTrue(guide['resume']['requires_complete_arguments'])
        self.assertFalse(guide['resume']['ready_to_call'])
        self.assertFalse(guide['mutation_authorized'])
        for value in guide['fields']:
            self.assertTrue(value['path'].startswith('/'))
            self.assertTrue(value['expected_format'])
        return guide

    def test_finance_collects_all_missing_inputs_without_echoing_facts(self):
        data=sample(); data['claim_amount']=None
        data['documents'][0]['dedup_key']=''
        data['facts'][0]['value']='synthetic-private-purpose'
        result=preflight(PreflightRequest.model_validate(data))
        guide=self.check_guide(result,'school_finance_preflight')
        self.assertEqual({v['code'] for v in guide['fields']},{'missing_claim_amount','missing_invoice_identity'})
        self.assertNotIn('synthetic-private',json.dumps(guide))
        self.assertEqual(guide['resume']['preserve_arguments'],['request'])
        self.assertIsNone(preflight(PreflightRequest.model_validate(sample()))['input_guidance'])

    def test_finance_facts_have_selectors_and_do_not_guess(self):
        result=preflight(PreflightRequest())
        guide=self.check_guide(result,'school_finance_preflight')
        selectors=[f['selector'] for f in guide['fields'] if 'selector' in f]
        self.assertIn({'field':'purpose'},selectors)
        self.assertIn({'field':'settlement'},selectors)

    def test_material_names_do_not_collide_with_fact_or_amount_fields(self):
        data=sample('remuneration'); data['documents']=data['documents'][:1]
        guide=preflight(PreflightRequest.model_validate(data))['input_guidance']
        material=next(f for f in guide['fields'] if f['code']=='missing_material')
        self.assertEqual(material['path'],'/request/documents')
        data=sample(); data['documents'][0]['document_id']='claim_amount'
        data['documents'][0]['dedup_key']=''
        data['lines'][0]['document_ids']=['claim_amount']
        guide=preflight(PreflightRequest.model_validate(data))['input_guidance']
        missing=next(f for f in guide['fields'] if f['code']=='missing_invoice_identity')
        self.assertEqual(missing['path'],'/request/documents')
        data=sample(); data['lines'][0].update(line_id='currency',currency='USD'); data['currency']='USD'
        guide=preflight(PreflightRequest.model_validate(data))['input_guidance']
        self.assertEqual({f['path'] for f in guide['fields'] if f['code']=='unsupported_currency'},
                         {'/request/lines','/request/currency'})

    def test_market_missing_fields_do_not_make_network_calls(self):
        publisher=Publisher.__new__(Publisher)
        result=publisher.prepare(description='synthetic-private-description')
        guide=self.check_guide(result,'school_nan7_prepare_offer')
        self.assertEqual({f['path'] for f in guide['fields']},{'/title','/contact','/category'})
        self.assertNotIn('synthetic-private',json.dumps(guide))
        self.assertIn('description',guide['resume']['preserve_arguments'])

    def test_bb_course_choice_resumes_at_supported_read_tool(self):
        with patch('school_mcp.bb.client.BBClient') as cls:
            cls.return_value.courses.return_value={'courses':[{'course_id':'_1_1'},{'course_id':'_2_1'}]}
            result=prepare_bb_by_name('synthetic','作业',files=['synthetic-private-file'])
        guide=self.check_guide(result,'school_bb_list_assignments')
        self.assertEqual(guide['fields'][0]['source'],'tool_result')
        self.assertNotIn('synthetic-private',json.dumps(guide))

    def test_bb_assignment_choice_preserves_files_and_target(self):
        with patch('school_mcp.bb.client.BBClient') as cls, patch('school_mcp.bb.assignments.catalog') as cat:
            cls.return_value.courses.return_value={'courses':[{'course_id':'_1_1'}]}
            cat.return_value={'assignments':[{'course_id':'_1_1','content_id':i,'title':'合成作业'} for i in ['_2_1','_3_1']]}
            result=prepare_bb_by_name('课程','作业',files=['synthetic-file'])
        guide=self.check_guide(result,'school_bb_prepare_submission')
        self.assertEqual(guide['resume']['arguments'],{'course_id':'_1_1'})
        self.assertIn('files',guide['resume']['preserve_arguments'])

    def test_jw_direct_and_combined_resume_differ(self):
        missing=candidate(1,required=True); missing.schedule_known=False
        direct=generate([missing],Constraints())
        guide=self.check_guide(direct,'school_jw_plan_timetables')
        self.assertEqual(guide['fields'][0]['source'],'official_data')
        client=Mock(); client.planning_context.return_value={'state':'context_ready','candidates':[]}
        client.offerings.return_value={'truncated':True}
        combined=plan_timetable(1,['synthetic'],Constraints(),client)
        guide=self.check_guide(combined,'school_jw_build_timetable')
        self.assertEqual(guide['fields'][0]['path'],'/keywords')
        self.assertIn('constraints',guide['resume']['preserve_arguments'])

    def test_resume_argument_names_exist_on_registered_tools(self):
        from school_mcp.bb.server import mcp as bb
        from school_mcp.jw.server import mcp as jw
        from school_mcp.nan7.server import mcp as nan7
        from school_mcp.finance.server import mcp as finance
        results=[preflight(PreflightRequest()),Publisher.__new__(Publisher).prepare(),
                 generate([candidate(1),candidate(1)],Constraints())]
        tools={tool.name:tool for m in [bb,jw,nan7,finance] for tool in m._tool_manager.list_tools()}
        for result in results:
            resume=result['input_guidance']['resume']
            parameters=inspect.signature(tools[resume['tool']].fn).parameters
            self.assertTrue(set(resume['preserve_arguments']) <= set(parameters))
            self.assertTrue(set(resume['arguments']) <= set(parameters))


if __name__=='__main__': unittest.main()
