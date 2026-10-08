"""Explicit local login command, with masked input and background Chrome."""
import getpass
import re
from urllib.parse import urlsplit, urljoin
from playwright.sync_api import sync_playwright
from ..browser import chrome_browser
from ..network import limiter
from ..workflow_store import require, WorkflowError
from .session import save_session


def run():
    username=getpass.getpass('评课社区账号（输入隐藏）: ')
    password=getpass.getpass('评课社区密码（输入隐藏）: ')
    require(username and password, '账号与密码不能为空。')
    limiter.acquire('icourse')
    try:
        with sync_playwright() as pw, chrome_browser(pw,headless=True) as browser:
            context=browser.new_context(service_workers='block',accept_downloads=False,java_script_enabled=False)
            writes=0
            destination=None
            def guard(route):
                nonlocal writes,destination
                r=route.request;p=urlsplit(r.url)
                allowed=p.scheme=='https' and p.netloc=='icourse.club' and not p.query
                if r.method=='POST':
                    allowed=allowed and p.path=='/signin/' and writes==0
                    if allowed:
                        writes+=1
                        # Browser routing may only see the first request of a
                        # redirect chain. Never forward a password via 307/308.
                        result=route.fetch(max_redirects=0)
                        headers=dict(result.headers)
                        if result.status in {302,303}:
                            target=urlsplit(urljoin(r.url,headers.get('location','')))
                            if not (target.scheme=='https' and target.netloc=='icourse.club' and target.path in {'/','/latest_reviews'} and not target.query and not target.fragment):
                                route.abort();return
                            destination=target.geturl()
                            headers.pop('location',None)
                            route.fulfill(response=result,status=200,headers=headers)
                        elif result.status==200:
                            route.fulfill(response=result)
                        else:
                            route.abort()
                        return
                else:
                    allowed=allowed and r.method=='GET' and (p.path in {'/signin/','/','/latest_reviews'} or p.path.startswith('/static/'))
                route.continue_() if allowed else route.abort()
            context.route('**/*',guard)
            page=context.new_page();page.goto('https://icourse.club/signin/',wait_until='domcontentloaded',timeout=30000)
            form=page.locator('form[action="/signin/"]')
            require(form.count()==1, '登录页面结构已变化。')
            form.locator('[name=username]').fill(username);form.locator('[name=password]').fill(password)
            with page.expect_navigation(wait_until='domcontentloaded',timeout=30000):
                form.locator('button[type=submit]').click()
            if destination:
                page.goto(destination,wait_until='domcontentloaded',timeout=30000)
            links=page.locator('a').evaluate_all("els=>els.filter(e=>e.textContent.trim()==='个人主页').map(e=>e.getAttribute('href'))")
            ids={m[1] for href in links if (m:=re.fullmatch(r'/user/(\d+)',href or ''))}
            require(len(ids)==1, '登录未完成，可能需要验证或账号激活；未自动重试。')
            save_session(context.cookies(),ids.pop(),page.evaluate('navigator.userAgent'))
        limiter.success('icourse')
        print('评课社区会话已加密保存。')
    except Exception:
        limiter.failure('icourse',authentication=True)
        raise WorkflowError('评课登录未完成。请核对本机输入或本人处理站点验证；没有自动注册或重试。') from None
    finally:
        password=username=''
