"""Explicit official login window for services sharing the campus session store."""
import re
import time
from urllib.parse import urlsplit
from .session import browser_session,ENTRIES
from ..bb.identity import IDENTITY_HOSTS
from ..desktop.auth import CAPTURE_ACCOUNT
from ..service_errors import ServiceError

SUPPORTED={'portal','student','spaces','drive','vlab'}


def manual(site,notify,stopped=None,timeout=600):
    if site not in SUPPORTED:raise ValueError('没有这个平台的登录入口。')
    with browser_session(site,headed=True) as s:
        observed={'account':s.account,'drive_ready':False}
        def capture(source,value):
            p=urlsplit(source['frame'].url)
            if p.scheme=='https' and p.hostname in IDENTITY_HOSTS and isinstance(value,str) and re.fullmatch('[A-Za-z0-9_.@-]{1,100}',value):
                observed['account']=value
        s.context.expose_binding('schoolLoginAccount',capture);s.context.add_init_script(CAPTURE_ACCOUNT)
        def response(r):
            if site=='drive' and r.url.split('?')[0]==s.base+'/api/v1/user/get_user_info':
                try:observed['drive_ready']=r.json().get('code')==1
                except ValueError:pass
        s.context.on('response',response)
        notify('waiting_for_user','请在学校官方窗口完成登录和验证；本应用只保留会话，不读取密码。')
        s.page.goto(ENTRIES[site],wait_until='domcontentloaded',timeout=45000)
        deadline=time.monotonic()+timeout;next_check=0
        while s.context.pages and time.monotonic()<deadline:
            if stopped and stopped.is_set():raise ServiceError('用户已停止登录。',code='cancelled')
            page=s.context.pages[-1];host=urlsplit(page.url).hostname
            if host==urlsplit(ENTRIES[site]).hostname and time.monotonic()>=next_check:
                next_check=time.monotonic()+3;ready=False;account=None
                try:
                    if site=='portal' and urlsplit(page.url).path=='/page/site/index':
                        value=s.get('/site/user_info',{'clenttype':'pc'}).json()
                        ready=value.get('e')==0;data=value.get('d') or {};account=data.get('user_number') or data.get('account')
                    elif site=='student':ready=page.get_by_text('奖学金申报',exact=True).count()>0
                    elif site=='spaces' and page.get_by_text('个人中心',exact=True).count():
                        value=s.get('/ic-web/auth/userInfo').json();ready=value.get('code')==0;account=(value.get('data') or {}).get('logonName')
                    elif site=='vlab' and page.locator('a[href="/vm/userinfo/"]').count():
                        ready=True;account=page.locator('a[href="/vm/userinfo/"]').inner_text().strip()
                    elif site=='drive':ready=observed['drive_ready']
                    if ready:
                        s.confirm(observed['account']);s.confirm(account)
                        notify('connected','本平台登录已核实，会话已在本机加密保存。')
                        return {'connected':True,'password_saved':False,'service':site}
                except ServiceError as exc:
                    if exc.code not in {'authentication_required','unavailable'}:raise
            page.wait_for_timeout(300)
    raise ServiceError('登录尚未完成，请回到原任务继续。',code='manual_verification_required')
