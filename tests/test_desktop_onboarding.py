"""Two-step setup and deterministic connection recovery; no real school/API traffic."""
import asyncio
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch, AsyncMock
import httpx

from school_mcp.desktop.app import Application
from school_mcp.desktop.chat import ChatRunner
from school_mcp.desktop.connections import connect, needs_connection
from school_mcp.desktop.model import ModelClient, ModelError, list_models, save_settings, test_connection
from school_mcp.desktop.storage import read_private
from school_mcp.bb.identity import save_session_identity
from school_mcp.agent.store import Store
from school_mcp.recovery import failure
from school_mcp.young.registration import RegistrationBrowser
from school_mcp.young.session import YoungError


CONFIG = {'base_url': 'https://model.example/v1', 'model': 'synthetic', 'api_key': 'synthetic-only'}


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ)
        env.start(); self.addCleanup(env.stop)
        self.app = Application(self.tmp.name)
        self.data = self.app.data
        save_settings(self.data, CONFIG)

    def test_full_url_normalization_and_key_never_follows_new_destination(self):
        public = save_settings(self.data, {**CONFIG, 'base_url': CONFIG['base_url']+'/chat/completions/'})
        self.assertEqual(public['base_url'], CONFIG['base_url'])
        self.assertNotIn('api_key', public)
        with self.assertRaises(ValueError):
            list_models(self.data, {**CONFIG, 'base_url':'https://other.example', 'api_key':''})
        transport = httpx.MockTransport(lambda req: httpx.Response(200, json={'data':[{'id':'synthetic-a'}, {'id':'synthetic-a'}, {'id':'synthetic-b'}, {'id':'bad\nvalue'}]}))
        result = list_models(self.data, {**CONFIG, 'api_key':''}, transport)
        self.assertEqual(result['models'], ['synthetic-a','synthetic-b'])
        self.assertNotIn(CONFIG['api_key'], json.dumps(result))

    def test_model_discovery_optional_and_no_redirect(self):
        result = list_models(self.data, CONFIG, httpx.MockTransport(lambda r:httpx.Response(404)))
        self.assertFalse(result['available'])
        seen=[]
        def redirect(request):
            seen.append(request)
            return httpx.Response(302, headers={'location':'https://other.example'})
        with self.assertRaises(ModelError):
            list_models(self.data, CONFIG, httpx.MockTransport(redirect))
        self.assertEqual(len(seen),1)

    def test_test_requires_reading_real_tool_result_and_keeps_reasoning(self):
        def complete(messages, tools, **kwargs):
            self.assertFalse(kwargs.get('force_tool'))
            if messages[-1]['role'] == 'tool':
                self.assertEqual(messages[-2]['reasoning_content'], 'synthetic reasoning')
                return {'role':'assistant','content':json.loads(messages[-1]['content'])['verification_code']}
            return {'role':'assistant','content':None,'reasoning_content':'synthetic reasoning','tool_calls':[
                {'id':'check','type':'function','function':{'name':'connection_check','arguments':'{"ok":true}'}}]}
        with patch.object(ModelClient,'complete',side_effect=complete):
            self.assertTrue(test_connection(self.data)['tool_result_read'])
        with patch.object(ModelClient,'complete',side_effect=[complete([{'role':'user'}],[]),{'role':'assistant','content':'unsupported'}]):
            with self.assertRaises(ModelError):test_connection(self.data)
        self.assertFalse(read_private(self.data.settings_path)['tested'])

    def test_reconnect_only_authentication_and_recheck_before_business(self):
        fake=Mock()
        fake.call_tool=AsyncMock(side_effect=[(None,failure(code='authentication_required')),(None,{'connected':True})])
        start=Mock(return_value={'started':True})
        with patch('school_mcp.desktop.connections.adapter',return_value=fake), patch('school_mcp.desktop.connections.importlib.import_module',return_value=Mock(start=start)), patch('school_mcp.desktop.connections.progress',return_value={'login_running':False,'login_progress':{'stage':'connected'}}):
            self.assertTrue(connect('bb',threading.Event(),Mock())['connected'])
        start.assert_called_once()
        self.assertEqual(fake.call_tool.await_count,2)
        for code in ('cooldown','timeout','access_denied','network_unavailable'):
            fake.call_tool=AsyncMock(return_value=(None,failure(code=code)))
            with patch('school_mcp.desktop.connections.adapter',return_value=fake), patch('school_mcp.desktop.connections.importlib.import_module') as module:
                self.assertEqual(connect('bb',threading.Event(),Mock())['error_code'],code)
                module.assert_not_called()

    def test_connection_before_tool_and_failure_does_not_dispatch(self):
        save_session_identity('synthetic-account')
        task=Store().create('bb','synthetic read',None)['task_id']
        chat=self.data.new_chat();chat['tasks']=[task]
        args={'task_id':task,'tool_name':'school_bb_list_courses','arguments':{}}
        ready=Mock(return_value=failure(code='manual_verification_required',service='bb'))
        runner=ChatRunner(self.data,threading.Event(),connect=ready)
        with patch('school_mcp.desktop.chat.mcp.call_tool',new_callable=AsyncMock) as call:
            result=asyncio.run(runner.dispatch(chat,'school_agent_call',args))
            self.assertFalse(result['dispatched']);call.assert_not_called()
        ready.return_value={'connected':True}
        with patch('school_mcp.desktop.chat.mcp.call_tool',new_callable=AsyncMock,return_value=(None,{'courses':[]})) as call:
            asyncio.run(runner.dispatch(chat,'school_agent_call',args))
            asyncio.run(runner.dispatch(chat,'school_agent_call',args))
            self.assertEqual(call.await_count,2)
        self.assertEqual(ready.call_count,2)  # one failure plus one successful per-turn connection
        self.assertFalse(needs_connection('finance','school_finance_preflight','read'))
        self.assertFalse(needs_connection('bb','school_bb_status','read'))

    def test_mail_not_required_for_chat_setup_and_key_change_invalidates_test(self):
        save_session_identity('synthetic-account')
        status=self.app.status()
        self.assertFalse(status['mail']['configured'])
        self.assertTrue(status['identity']['saved'])
        save_settings(self.data,{**CONFIG,'model':'other'})
        self.assertFalse(self.app.status()['model']['tested'])


class ProjectListingTests(unittest.TestCase):
    def browser(self, batches):
        b=RegistrationBrowser({})
        index=[0]
        def response():
            return Mock(json=lambda:{'success':True,'result':{'records':batches[index[0]]}})
        button=Mock()
        button.get_attribute.side_effect=lambda _: 'ant-pagination-disabled' if index[0]==len(batches)-1 else ''
        button.click.side_effect=lambda:index.__setitem__(0,index[0]+1)
        class Expect:
            def __enter__(self):return self
            def __exit__(self,*_):pass
            @property
            def value(self):return response()
        b.page=Mock()
        b.page.locator.return_value=button
        b.page.expect_response.return_value=Expect()
        b.start_listing=Mock(side_effect=response)
        return b

    def test_page_count_and_next_do_not_imply_eligibility(self):
        b=self.browser([[{'id':'one','itemName':'合成项目'}],[{'id':'two','itemName':'合成项目二'}]])
        r=b.listing_page(1)
        self.assertEqual((r['page'],r['count'],r['next_page']),(1,1,2))
        b.page.locator.return_value.click.assert_not_called()
        r=b.listing_page(2)
        self.assertEqual(r['projects'][0]['id'],'two')
        self.assertFalse(r['has_next'])
        self.assertIsNone(r['next_page'])
        self.assertIn('不代表个人报名资格',r['scope'])

    def test_pagination_stagnation_and_bad_response_never_become_empty_success(self):
        b=self.browser([[{'id':'one'}],[{'id':'one'}]])
        with self.assertRaises(YoungError):b.listing_page(2)
        b=self.browser([[]])
        b.start_listing.return_value=Mock(json=lambda:{'success':False})
        b.start_listing.side_effect=None
        with self.assertRaises(YoungError):b.listing_page(1)
        b=self.browser([[]])
        self.assertEqual(b.listing_page(1)['count'],0)
        with self.assertRaises(YoungError):b.listing_page(2)


if __name__=='__main__':unittest.main()
