"""Actual agent stdio lifecycle in a disposable, credential-free private directory."""
import asyncio
import os
import sys
import tempfile
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    with tempfile.TemporaryDirectory() as directory:
        env = {**os.environ, 'SCHOOL_MCP_LOCAL_DIR': directory, 'SCHOOL_MAIL_PASSWORD': '', 'PYTHONUTF8': '1'}
        async def connect(resume=None):
            parameters = StdioServerParameters(command=sys.executable, args=['-m', 'school_mcp', 'agent-serve'], env=env)
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert len(tools.tools) == 7
                    async def call(name, arguments):
                        result = await session.call_tool('school_agent_' + name, arguments)
                        assert not result.isError, result
                        return result.structuredContent
                    if resume:
                        context = await call('task', {'task_id': resume, 'include_context': True})
                        assert context['call_count'] == 1 and context['state'] == 'needs_input'
                        assert context['calls'][0]['arguments']['request']['business_type'] == 'travel'
                        return resume
                    routed = await call('route', {'goal': '准备差旅报销'})
                    assert routed['candidates'][0]['workflow'] == 'finance'
                    task = await call('start', {'workflow': 'finance', 'goal': 'synthetic travel preparation'})
                    identifier = task['task_id']
                    schema = await call('tools', {'task_id': identifier, 'tool_name': 'school_finance_preflight'})
                    assert schema['schema_included']
                    result = await call('call', {'task_id': identifier, 'tool_name': 'school_finance_preflight',
                                                'arguments': {'request': {'business_type': 'travel'}}})
                    assert result['dispatched'] and result['state'] == 'needs_input'
                    assert result['result']['safe_to_submit'] is False
                    assert result['result']['input_guidance']['fields']
                    assert (await call('tasks', {}))['tasks'][0]['task_id'] == identifier
                    return identifier
        task = await connect()
        await connect(task)
        print('agent stdio: route, schema, real offline preflight, missing inputs, encrypted restart recovery passed; no school writes')


if __name__ == '__main__':
    asyncio.run(main())
