"""Actual stdio calls following only documented states; not a model evaluation."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def scenario(service, failure=''):
    with tempfile.TemporaryDirectory() as d:
        env={**os.environ,'SCHOOL_MCP_LOCAL_DIR':d,'SCHOOL_MAIL_PASSWORD':'','PYTHONUTF8':'1'}
        args=[str(Path(__file__).with_name('business_workflow_server.py')),service]
        if failure:args.append(failure)
        async with stdio_client(StdioServerParameters(command=sys.executable,args=args,env=env)) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                async def call(name,arguments):
                    result=await session.call_tool(name,arguments)
                    assert not result.isError,result
                    return result.structuredContent
                if service=='jw':
                    candidate={'lesson_id':'1','course_code':'A','course_name':'synthetic','credits':2,'schedule_known':True,'required':True,'slots':[{'weekday':1,'start_period':1,'end_period':2,'weeks':[1,3]}]}
                    v=await call('school_jw_plan_timetables',{'candidates':[candidate],'constraints':{'min_credits':2,'max_credits':2}})
                    assert v['state']=='planned' and not v['enrollment_performed']
                    candidate['slots']=[];candidate['schedule_known']=False
                    v=await call('school_jw_plan_timetables',{'candidates':[candidate],'constraints':{}})
                    assert v['state']=='needs_input' and not v['plans']
                else:
                    if service=='nan7':
                        v=await call('school_nan7_prepare_offer',{})
                        assert v['state']=='needs_input' and v['next_tool'] is None
                        p=await call('school_nan7_prepare_offer',{'title':'synthetic book','description':'合成测试描述','price':'10','contact':'synthetic@example.com','category':1})
                    else:
                        options=await call('school_icourse_review_options',{'course_id':1})
                        assert options['can_create_new']
                        p=await call('school_icourse_prepare_review',{'course_id':1,'term':options['terms'][0]['id'],'content':'这是合成评课内容，不代表真实修课经历。'})
                    assert p['state']=='ready' and p['authorization_required']
                    # Synthetic request explicitly authorizes this exact fixture publication.
                    result=await call(p['next_tool'],p['next_arguments'])
                    if failure:
                        assert result['state']=='uncertain' and not result['retry_write_allowed']
                        result=await call(result['next_tool'],result['next_arguments'])
                        assert result['state']==('verified' if service=='icourse' else 'uncertain')
                    else:
                        assert result['state']=='verified',result
                    repeated=await call(p['next_tool'],p['next_arguments'])
                    assert repeated['state']==result['state']
                print(json.dumps({'service':service,'scenario':failure or 'success','actual_stdio':'passed','real_site_writes':0}))


async def main():
    for service,failure in [('jw',''),('nan7',''),('nan7','lost'),('icourse',''),('icourse','lost')]:
        await scenario(service,failure)

if __name__=='__main__':asyncio.run(main())
