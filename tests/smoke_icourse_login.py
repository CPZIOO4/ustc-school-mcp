"""Real background Chrome + DPAPI, with all HTTP replaced by synthetic pages."""
import json
import os
import tempfile
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch
from types import SimpleNamespace

from school_mcp.browser import chrome_browser
from school_mcp.icourse import login
from school_mcp.icourse.session import load_session
from school_mcp.workflow_store import WorkflowError
from pathlib import Path


def run_case(redirect_target='/latest_reviews',redirect_status=302,success=True):
    requests=[]
    class ContextProxy:
        def __init__(self,context):self.context=context
        def __getattr__(self,name):return getattr(self.context,name)
        def route(self,pattern,guard):
            def intercept(route):
                class RouteProxy:
                    request=route.request
                    def abort(self):route.abort()
                    def fetch(self,**kwargs):
                        assert kwargs=={'max_redirects':0}
                        r=route.request;requests.append((r.method,urlsplit(r.url).path))
                        payload=parse_qs(r.post_data)
                        assert payload['username']==['synthetic-user'] and payload['password']==['synthetic-password']
                        return SimpleNamespace(status=redirect_status,headers={'location':redirect_target,'set-cookie':'session=synthetic-cookie; Path=/; Secure; HttpOnly'})
                    def fulfill(self,**kwargs):
                        result=kwargs.pop('response')
                        assert kwargs['status']==200 and 'location' not in kwargs['headers']
                        route.fulfill(content_type='text/html; charset=utf-8',body='<p>synthetic transition</p>',**kwargs)
                    def continue_(self):
                        r=route.request;p=urlsplit(r.url);requests.append((r.method,p.path))
                        if r.method=='GET' and p.path=='/signin/':
                            route.fulfill(status=200,content_type='text/html; charset=utf-8',body='<form method="post" action="/signin/"><input name="username"><input type="password" name="password"><input name="csrf_token" value="synthetic-csrf"><button type="submit">登录</button></form>')
                        elif r.method=='GET' and p.path=='/latest_reviews':
                            route.fulfill(status=200,content_type='text/html; charset=utf-8',body='<a href="/user/7">个人主页</a>')
                        else:
                            route.abort()
                guard(RouteProxy())
            self.context.route(pattern,intercept)
    class BrowserProxy:
        def __init__(self,browser):self.browser=browser
        def new_context(self,**kwargs):
            assert kwargs['java_script_enabled'] is False
            return ContextProxy(self.browser.new_context(**kwargs))
    @contextmanager
    def browser(pw,*,headless=None):
        assert headless is True
        with chrome_browser(pw,headless=True) as b:yield BrowserProxy(b)
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'SCHOOL_MCP_LOCAL_DIR':directory}), patch.object(login,'chrome_browser',browser), patch.object(login,'limiter',Mock()), patch.object(login.getpass,'getpass',side_effect=['synthetic-user','synthetic-password']):
        if success:
            login.run()
            s=load_session()
            assert s['user_id']=='7' and s['cookies'][0]['domain']=='icourse.club'
        else:
            try:
                login.run()
            except WorkflowError:
                pass
            else:
                raise AssertionError('unsafe redirect accepted')
            assert not (Path(directory)/'icourse-session.dpapi').exists()
        assert requests.count(('POST','/signin/'))==1
        assert all(path in {'/signin/','/latest_reviews'} for _,path in requests)
    print(json.dumps({'synthetic_headless_chrome_login':'passed' if success else 'unsafe_redirect_blocked','real_site_requests':0,'redirect_status':redirect_status}))


if __name__=='__main__':
    run_case()
    run_case('https://example.com/collect',302,False)
    run_case('/latest_reviews',307,False)
