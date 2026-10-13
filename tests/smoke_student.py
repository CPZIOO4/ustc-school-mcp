"""Headless Chrome exercises the actual scholarship adapter on a synthetic site.

Every network request is intercepted. No school identity or write is used.
"""
import html
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from urllib.parse import parse_qs,urlsplit
from unittest.mock import patch
from contextlib import contextmanager

from playwright.sync_api import sync_playwright
from school_mcp.student import applications as a
from school_mcp.workflow_store import PlanStore
from test_student_applications import TARGET,values


def main():
    out=Path('.local/student-application-ui');out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'SCHOOL_MCP_LOCAL_DIR':tmp}), \
            patch.object(a,'load_credentials',return_value={'username':'synthetic-account'}), \
            patch.object(a.limiter,'acquire'), sync_playwright() as pw:
        browser=pw.chromium.launch(channel='chrome',headless=True)
        state={'opened':0,'submits':0,'saved':{},'submitted':False,'checkboxes':[]}
        fields=values(); fields['cadre']='无'; fields['clubs']='无'
        # These are fixture IDs, unrelated to a user's school data.
        checks={'check-model':'美国大学生数模竞赛','check-service':'志愿服务','check-math':'中国大学生数学竞赛'}
        def page_html():
            parts=['<html><body><input id="oScholarShipItemTypeName" value="'+a.TITLE+'" readonly>',
                   '<input id="Text3" value="synthetic-account" readonly>',
                   '<input fieldname="BelongToClassID" value="synthetic-class" ismustfill="1">',
                   '<input id="oApplyMoney" readonly>']
            for key,(identifier,_,required) in a.FIELDS.items():
                value=state['saved'].get(identifier,'')
                attrs=f'id="{identifier}" ismustfill="{1 if required else 0}" onchange="saveField(this)"'
                if key in {'nation','political_status'}:
                    label=fields[key];selected=' selected' if value==label else ''
                    parts.append(f'<select {attrs}><option value=""></option><option{selected}>{label}</option></select>')
                elif key=='entry_month':
                    parts.append(f'<input {attrs} value="{html.escape(value)}" readonly onfocus="showMonth()">')
                else:parts.append(f'<textarea {attrs}>{html.escape(value)}</textarea>')
            for identifier,label in checks.items():
                checked=' checked' if identifier in state['checkboxes'] else ''
                parts.append(f'<table><tr><td><input type="checkbox" id="{identifier}"{checked} onclick="saveChecks()"></td><td>{label}；</td></tr></table>')
            for identifier in ('Text13','Text10','Text11','Text12','Text14','oScholarShipAdjunctID'):
                parts.append(f'<input id="{identifier}" value="">')
            parts += ['<input type="button" value="暂 存" onclick="saveAll()">',
                      '<input type="button" value="提 交" onclick="submitForm()">',
                      '<iframe name="YMLayer" style="display:none"></iframe>', '''<script>
            function call(fun,data) {let x=new XMLHttpRequest();x.open('POST','pbApplyTableSave.aspx',false);x.send('funID='+fun+'&data='+encodeURIComponent(JSON.stringify(data)));return x.responseText}
            function saveField(e){call(2,{id:e.id,value:e.value})}
            function saveChecks(){call(2,{checks:[...document.querySelectorAll('input:checked')].map(e=>e.id)})}
            function saveAll(){call(2,{});alert('保存成功')}
            function submitForm(){if(confirm('提交之后，将不可修改，确定要提交吗')){call(4,{});alert('提交成功！');location.href='pbApplyTableFinish.aspx?itemID=1001'}}
            function showMonth(){let f=document.querySelector('iframe');f.style.display='block';let d=f.contentDocument;d.open();d.write('<span id="yearSpan">2026年</span><button title="向前翻 1 年" onclick="parent.shift(-1)">前</button><button title="向后翻 1 年" onclick="parent.shift(1)">后</button><button onclick="parent.month()">9月</button>');d.close()}
            function shift(n){let s=document.querySelector('iframe').contentDocument.getElementById('yearSpan');s.textContent=(parseInt(s.textContent)+n)+'年'}
            function month(){let f=document.querySelector('iframe'),e=document.getElementById('Text4');e.value=parseInt(f.contentDocument.getElementById('yearSpan').textContent)+'-09';saveField(e);f.style.display='none'}
            </script></body></html>''']
            return ''.join(parts)
        def route(route):
            request=route.request;p=urlsplit(request.url)
            assert p.hostname==a.HOST,'unexpected host'
            if p.path==a.SAVE:
                data=parse_qs(request.post_data);payload=json.loads(data['data'][0])
                if data['funID']==['2']:
                    if 'id' in payload:state['saved'][payload['id']]=payload['value']
                    if 'checks' in payload:state['checkboxes']=payload['checks']
                else:
                    assert data['funID']==['4'];state['submits']+=1;state['submitted']=True
                return route.fulfill(body='ok')
            if p.path==a.FORM:
                state['opened']+=1;return route.fulfill(content_type='text/html; charset=utf-8',body=page_html())
            if p.path==a.FINISH:return route.fulfill(content_type='text/html; charset=utf-8',body='申请已提交')
            if p.path in a.PAGES.values():
                show=p.path==a.PAGES['applied'] and bool(state['opened']) or p.path==a.PAGES['open'] and not state['opened']
                status='个人已提交' if state['submitted'] else '个人未提交' if state['opened'] else '点击申请'
                row=f'<tr><td>1</td><td>{a.TITLE}</td><td>{TARGET["year"]}</td><td>{TARGET["starts"]}</td><td>{TARGET["ends"]}</td><td><a href="pbApplyTable.aspx?itemID=1001">{status}</a></td></tr>' if show else ''
                return route.fulfill(content_type='text/html; charset=utf-8',body='<table id="oHtmlTable">'+row+'</table>')
            route.abort()
        @contextmanager
        def synthetic_browser():
            context=browser.new_context(service_workers='block');context.route('**/*',route)
            page=context.new_page()
            def goto(path=None):page.goto('https://'+a.HOST+path if path.startswith('/') else path,wait_until='domcontentloaded');return page
            session=SimpleNamespace(context=context,page=page,account='synthetic-account',goto=goto)
            with patch.object(a,'connected',return_value=page),patch.object(a,'enter_scholarships'):
                try:yield a.BrowserApplication(session)
                finally:context.close()
        ref=a.remember('synthetic-account',a.TITLE,[TARGET['year'],TARGET['starts'],TARGET['ends'],'点击申请'],[('申请',TARGET['url'])])['application_reference']
        prepared=a.prepare(ref,fields,['美国大学生数模竞赛','志愿服务'])
        assert state['opened']==0,'prepare must not create a school draft'
        with patch.object(a,'browser',synthetic_browser):
            result=a.submit(prepared['plan_id'],prepared['content_sha256'],True)
            assert result['state']=='verified',result
            assert state['submits']==1
            assert state['saved']['Text4']=='2023-09'
            assert set(state['checkboxes'])=={'check-model','check-service'}
            assert a.submit(prepared['plan_id'],prepared['content_sha256'],True)['state']=='verified'
            assert state['submits']==1
            newer=a.prepare(ref,fields|{'cadre':'合成变更'},[])
            opens=state['opened']
            assert a.submit(newer['plan_id'],newer['content_sha256'],True)['state']=='already_submitted'
            assert state['opened']==opens
        browser.close()
        result={'prepare_without_school_write':True,'calendar_picker':True,'autosave_reload':True,
                'activities_verified':True,'submit_and_list_readback':True,'no_replay':True,
                'submitted_form_never_reopened':True,'school_network_used':False}
        (out/'result.json').write_text(json.dumps(result),encoding='utf-8');print(json.dumps(result))

if __name__=='__main__':main()
