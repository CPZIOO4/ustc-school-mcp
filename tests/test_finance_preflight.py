import copy
import json
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from school_mcp.finance.preflight import PreflightRequest, preflight


def sample(kind='daily'):
    facts = [{'field':key,'value':value,'confirmed':True} for key,value in {
        'purpose': {'daily':'办公用品','travel':'出差','loan':'借款','remuneration':'酬金','internal_transfer':'校内转账'}[kind],
        'funding_source':'synthetic-funding', 'contact':'已在本地确认', 'settlement':'已在本地确认',
        'travel_route':'合成城市甲至乙','travel_dates':'2026-10-01 至 2026-10-02','allowance_basis':'不申请补贴',
        'loan_category':'合成类别','borrower':'已确认','remuneration_category':'合成类别','income_period':'2026-10',
        'personnel_list':'合成人员清单','transfer_target':'synthetic-target','transfer_type':'合成类型'}.items()]
    line_kind={'daily':'invoice','travel':'invoice','loan':'loan_detail','remuneration':'payroll','internal_transfer':'transfer_detail'}[kind]
    documents=[{'document_id':'doc1','kind':'invoice' if line_kind=='invoice' else 'other',
                'available':True,'confirmed':True,'status':'normal','dedup_key':'synthetic-number'}]
    if kind=='travel': documents.append({'document_id':'route','kind':'itinerary','available':True,'confirmed':True})
    if kind=='remuneration': documents.append({'document_id':'people','kind':'personnel_list','available':True,'confirmed':True})
    return {'business_type':kind, 'facts':facts, 'claim_amount':'0.30','contract_required':False,
            'lines':[{'line_id':'line1','kind':line_kind,'amount':'0.30','document_amount':'0.30',
                      'confirmed':True,'document_ids':['doc1']}], 'documents':documents}


class FinancePreflightTests(unittest.TestCase):
    def run_check(self, data):
        return preflight(PreflightRequest.model_validate(data))

    def codes(self, data):
        return {x['code'] for x in self.run_check(data)['issues']}

    def test_all_five_routes_are_offline_and_never_authorize_submission(self):
        with patch('httpx.Client', side_effect=AssertionError('network')):
            for kind in ['daily','travel','loan','remuneration','internal_transfer']:
                result=self.run_check(sample(kind))
                self.assertEqual(result['state'],'ready_for_manual_review',result['issues'])
                self.assertFalse(result['safe_to_submit'])
                self.assertFalse(result['invoice_authenticity_verified'])
                self.assertFalse(result['network_checked'])
                self.assertFalse(result['files_read'])
                self.assertEqual(result['amounts']['claim_minus_lines'],'0.00')

    def test_auto_needs_choice_for_single_multiple_or_unknown_candidates(self):
        for text, expected in [('出差借款',{'travel','loan'}),('办公用品',{'daily'}),('其他事项',set())]:
            result=self.run_check({'facts':[{'field':'purpose','value':text,'confirmed':True}]})
            self.assertIsNone(result['business_type'])
            self.assertEqual({x['business_type'] for x in result['business_candidates']},expected)
            self.assertIn('choose_business_type',{x['code'] for x in result['issues']})

    def test_explicit_route_is_not_overridden_by_purpose_keywords(self):
        data=sample('loan'); data['facts'][0]['value']='出差前借款用于办公用品'
        self.assertEqual(self.run_check(data)['state'],'ready_for_manual_review')
        data['facts'][0]['value']='出差前预支经费'
        result=self.run_check(data)
        self.assertEqual(result['business_type'],'loan')
        self.assertEqual(result['state'],'ready_for_manual_review')
        self.assertEqual([x['business_type'] for x in result['business_candidates']],['travel'])

    def test_missing_and_unconfirmed_information_stays_blocked(self):
        data=sample(); data['facts']=[{'field':'purpose','value':'办公用品'}]
        self.assertTrue({'missing_information','unconfirmed_information'} <= self.codes(data))
        data=sample(); data['facts'].append(copy.deepcopy(data['facts'][0]))
        self.assertIn('duplicate_fact',self.codes(data))

    def test_decimal_sum_partial_invoice_and_mismatch(self):
        data=sample('loan'); data['lines'][0]['amount']='0.10'
        data['lines'].append({**data['lines'][0],'line_id':'line2','amount':'0.20'})
        result=self.run_check(data)
        self.assertEqual(result['amounts']['observed_line_totals'],{'CNY':'0.30'})
        self.assertEqual(result['state'],'ready_for_manual_review')
        data['claim_amount']='0.31'
        self.assertIn('amount_mismatch',self.codes(data))
        self.assertEqual(self.run_check(data)['amounts']['claim_minus_lines'],'0.01')
        data=sample(); data['lines'][0]['document_amount']='1.00'
        self.assertEqual(self.run_check(data)['state'],'ready_for_manual_review')
        data['lines'][0]['document_amount']='0.20'
        self.assertIn('exceeds_document_amount',self.codes(data))

    def test_unknown_and_foreign_amounts_are_not_filled_or_converted(self):
        data=sample(); data['lines'][0]['amount']=None
        result=self.run_check(data)
        self.assertFalse(result['amounts']['amounts_complete'])
        self.assertIsNone(result['amounts']['claim_minus_lines'])
        data=sample(); data['lines'][0]['currency']='USD'
        self.assertIn('unsupported_currency',self.codes(data))
        self.assertIsNone(self.run_check(data)['amounts']['claim_minus_lines'])

    def test_strict_inputs_reject_floats_scientific_notation_and_extra_actions(self):
        for value in [1.1,True,'NaN','1e3','-1','0.001','1,000','  ']:
            data=sample(); data['claim_amount']=value
            with self.assertRaises(ValidationError): PreflightRequest.model_validate(data)
        with self.assertRaises(ValidationError): PreflightRequest.model_validate({'submit':True})
        with self.assertRaises(ValidationError): PreflightRequest.model_validate({'facts':[{'field':'purpose','confirmed':'yes'}]})

    def test_duplicates_and_reused_invoice_do_not_silently_reduce_totals(self):
        data=sample(); data['documents'].append({**data['documents'][0],'document_id':'doc2', 'dedup_key':'ＳＹＮＴＨＥＴＩＣ-ＮＵＭＢＥＲ'})
        self.assertIn('possible_duplicate_invoice',self.codes(data))
        data=sample(); data['lines'].append({**data['lines'][0],'line_id':'line2'}); data['claim_amount']='0.60'
        result=self.run_check(data)
        self.assertIn('invoice_reused',{x['code'] for x in result['issues']})
        self.assertEqual(result['amounts']['observed_line_totals']['CNY'],'0.60')
        data['lines'][1]['line_id']='line1'
        self.assertIn('duplicate_line_id',self.codes(data))
        data['documents'].append(copy.deepcopy(data['documents'][0]))
        self.assertIn('duplicate_document_id',self.codes(data))

    def test_invalid_missing_or_wrong_evidence_stops(self):
        for state in ['void','red','unknown']:
            data=sample(); data['documents'][0]['status']=state
            self.assertIn('unusable_document',self.codes(data))
        data=sample(); data['lines'][0]['document_ids']=['absent']
        self.assertIn('unknown_document',self.codes(data))
        data=sample(); data['documents'][0]['kind']='contract'
        self.assertIn('invoice_reference_mismatch',self.codes(data))
        data=sample(); data['lines'][0]['document_ids']=[]
        self.assertIn('missing_amount_evidence',self.codes(data))

    def test_missing_invoice_identity_cannot_be_treated_as_checked(self):
        for missing in ['', ' \t\u3000 ']:
            data=sample(); data['documents'][0]['dedup_key']=missing
            result=self.run_check(data)
            self.assertEqual(result['state'],'needs_input')
            self.assertIn('missing_invoice_identity',{x['code'] for x in result['issues']})
            self.assertFalse(result['duplicate_check']['identities_complete'])
            self.assertFalse(result['duplicate_check']['history_checked'])
        result=self.run_check(sample())
        self.assertTrue(result['duplicate_check']['identities_complete'])
        self.assertEqual(result['duplicate_check']['scope'],'provided_documents_only')

    def test_unconfirmed_and_missing_files_are_not_usable(self):
        for field in ['available','confirmed']:
            data=sample(); data['documents'][0][field]=False
            self.assertIn('unusable_document',self.codes(data))
        data=sample(); data['lines'][0]['confirmed']=False
        self.assertIn('unconfirmed_amount',self.codes(data))

    def test_zero_values_wrong_line_type_and_missing_invoice_amount_stop(self):
        data=sample(); data['lines'][0]['amount']='0'; data['claim_amount']='0'
        self.assertTrue({'nonpositive_amount','nonpositive_claim_amount'} <= self.codes(data))
        data=sample(); data['lines'][0]['kind']='payroll'
        self.assertIn('line_kind_mismatch',self.codes(data))
        data=sample(); data['lines'][0]['document_amount']=None
        self.assertIn('missing_document_amount',self.codes(data))

    def test_route_materials_and_conditional_contracts(self):
        for kind in ['travel','remuneration']:
            data=sample(kind); data['documents']=data['documents'][:1]
            self.assertIn('missing_material',self.codes(data))
        data=sample(); data['contract_required']=None
        self.assertIn('confirm_contract_requirement',self.codes(data))
        data['contract_required']=True
        self.assertIn('missing_material',self.codes(data))
        data['documents'].append({'document_id':'contract','kind':'contract','available':True,'confirmed':True})
        self.assertEqual(self.run_check(data)['state'],'ready_for_manual_review')

    def test_output_omits_personal_facts_and_invoice_numbers(self):
        data=sample(); data['facts'][1]['value']='synthetic-private-funding'
        data['documents'][0]['dedup_key']='synthetic-private-invoice'
        output=json.dumps(self.run_check(data))
        self.assertNotIn('synthetic-private-',output)
        self.assertNotIn('dedup_key',output)


if __name__=='__main__': unittest.main()
