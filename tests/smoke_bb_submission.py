"""Actual MCP + headless Chrome against synthetic, intercepted school pages."""
import asyncio
import functools
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from school_mcp.bb.submission_browser import SubmissionBrowser

A = {'course_id': '_1_1', 'content_id': '_2_1', 'title': '合成作业', 'description': 'synthetic', 'due_text': [],
     'entry_path': '/webapps/assignment/uploadAssignment?course_id=_1_1&content_id=_2_1&mode=view'}
DATA = b'%PDF-synthetic-browser-test'
FORM = '''<html><h1 id="pageTitleHeader">上载作业：合成作业</h1><div id="contentPanel">截止日期 2099年1月1日 下午11:59 满分 100
<form id="uploadAssignmentFormId" action="/webapps/assignment/uploadAssignment?action=submit" method="post" enctype="multipart/form-data">
<input name="course_id" value="_1_1" type="hidden"><input name="content_id" value="_2_1" type="hidden">
<input name="attempt_id" value="" type="hidden"><input name="remove_file_id" value="" type="hidden">
<input name="dispatch" value="" type="hidden"><textarea id="studentSubmission.text" name="studentSubmission.text"></textarea>
<textarea id="student_commentstext" name="student_commentstext"></textarea><input id="newFile_chooseLocalFile" type="file" multiple>
<input name="bottom_提交" value="提交" type="submit" onclick="checkDupeFile();return false;">
</form></div><script>
window.newFile_FilePickerObject={getPickedFiles:()=>Array.from(document.getElementById('newFile_chooseLocalFile').files).map(f=>({file:f}))};
window.checkDupeFile=()=>{let f=document.getElementById('uploadAssignmentFormId');f.elements.dispatch.value='submit';let data=new FormData(f);
Array.from(document.getElementById('newFile_chooseLocalFile').files).forEach((file,i)=>data.append('newFile_LocalFile'+i,file));
fetch(f.action,{method:'POST',body:data});};</script></html>'''


def history(attempt):
    return f'''<h1 id="pageTitleHeader">复查提交历史记录: 合成作业</h1>
    <div id="assignmentInfo"><h3>截止日期</h3><p>2099年1月1日 下午11:59</p></div>
    <h3 id="currentAttempt_label"><span class="mainLabel">尝试</span><span class="dateStamp">synthetic time</span></h3>
    <ul id="currentAttempt_submissionList"><li><a class="attachment">original.pdf</a>
    <a class="dwnldBtn" href="/webapps/assignment/download?course_id=_1_1&amp;attempt_id={attempt}&amp;file_id=_5_1"></a></li></ul>
    <p id="bottom_submitButtonRow"><input value="开始新的" onclick="document.location='/webapps/assignment/uploadAssignment?action=newAttempt&amp;course_id=_1_1&amp;content_id=_2_1';"></p>'''


class SyntheticBrowser(SubmissionBrowser):
    posts = 0
    starts = 0
    def __init__(self):
        super().__init__(self.install)
    def install(self, context):
        def transport(route):
            request = route.request; p = urlsplit(request.url); q = parse_qs(p.query)
            if p.path == '/webapps/assignment/uploadAssignment':
                if request.method == 'GET' and q.get('mode') == ['view']:
                    if os.environ.get('BB_FIXTURE_FIRST') == '1' and not self.posts:
                        route.fulfill(status=200, content_type='text/html; charset=utf-8', body=FORM); return
                    route.fulfill(status=200, content_type='text/html; charset=utf-8', body=history('_4_1' if self.posts else '_3_1')); return
                if request.method == 'GET' and q.get('action') == ['newAttempt']:
                    type(self).starts += 1
                    route.fulfill(status=200, content_type='text/html; charset=utf-8', body=FORM); return
                if request.method == 'POST' and q == {'action': ['submit']}:
                    type(self).posts += 1
                    route.fulfill(status=200, content_type='application/json', body='{"ok":true}'); return
            route.abort()  # No real network access is possible through this fixture.
        context.route('**/*', transport)
    def download(self, *args): return DATA


def serve():
    from school_mcp.bb import server, submissions
    server.prepare_submission = functools.partial(submissions.prepare_submission, browser_factory=SyntheticBrowser)
    server.submit_assignment = functools.partial(submissions.submit_assignment, browser_factory=SyntheticBrowser)
    server.submission_status = functools.partial(submissions.submission_status, browser_factory=SyntheticBrowser)
    with patch.object(submissions, 'identity', return_value='synthetic'), \
         patch.object(submissions, 'session_binding', return_value='synthetic-session'), \
         patch.object(submissions, 'locate', return_value=A), \
         patch('school_mcp.bb.submission_browser.load_session', return_value={'cookies': []}):
        server.run()
    assert SyntheticBrowser.posts == 1 and SyntheticBrowser.starts == (0 if os.environ.get('BB_FIXTURE_FIRST') == '1' else 1)


async def scenario(first=False):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    with tempfile.TemporaryDirectory() as directory:
        env = {**os.environ, 'SCHOOL_MCP_LOCAL_DIR': directory, 'PYTHONUTF8': '1'}
        if first: env['BB_FIXTURE_FIRST'] = '1'
        file = Path(directory) / 'original.pdf'; file.write_bytes(DATA)
        parameters = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).resolve()), '--server'], env=env)
        async with stdio_client(parameters) as (reader, writer), ClientSession(reader, writer) as session:
            await session.initialize()
            args = {'course_id':'_1_1','content_id':'_2_1','comment':'synthetic\ncomment'}
            args.update({'files':[str(file)]} if first else {'reuse_previous_files':True,'resubmit':True})
            prepare = await session.call_tool('school_bb_prepare_submission', args)
            assert not prepare.isError, prepare
            p = prepare.structuredContent
            result = await session.call_tool('school_bb_submit_assignment', {'preparation_id':p['preparation_id'],'expected_sha256':p['content_sha256']})
            assert not result.isError and result.structuredContent['state'] == 'verified', result
            again = await session.call_tool('school_bb_submit_assignment', {'preparation_id':p['preparation_id'],'expected_sha256':p['content_sha256']})
            assert again.structuredContent['state'] == 'verified'
            status = await session.call_tool('school_bb_submission_status', {'preparation_id':p['preparation_id'],'verify':True})
            assert status.structuredContent['result']['files_match']
            print(json.dumps({'synthetic_stdio_chrome_flow':'passed','first_submission':first,'real_school_writes':0,'duplicate_submit':'not_replayed'}))


async def main():
    await scenario()
    await scenario(first=True)


if __name__ == '__main__':
    serve() if '--server' in sys.argv else asyncio.run(main())
