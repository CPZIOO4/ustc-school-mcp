"""Bounded, fixed read commands through the account's official SCOW shell."""
import re
import shlex
import uuid
from urllib.parse import urlsplit
from .client import BASE, connected, date_range
from ..service_errors import ServiceError


def command(job_id=None):
    if job_id is not None and (type(job_id) is not int or job_id <= 0):
        raise ServiceError('请提供有效的本人作业编号。', code='invalid_arguments')
    query = ('sacct -X -S ' + date_range(31)[0][:10] + ' -j ' + str(job_id) + ' --format=JobID,JobName%24,State,Elapsed,AllocCPUS,AllocTRES%60 --noheader') if job_id else 'squeue -u "$(id -un)" --format="%.12i %.24j %.12T %.12M %.6C %b"'
    return 'hostname && id -un && ' + query


def shell_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or p.hostname != '107.ustc.edu.cn' or p.port not in (None,443) or p.username or p.password or p.query or p.fragment or not re.fullmatch(r'/shell/training/[0-9.]+', p.path):
        raise ServiceError('终端地址不属于107官方本科生集群。', code='identity_mismatch')
    return url


def completed(text, marker):
    matches=list(re.finditer(r'(?m)^'+re.escape(marker)+r':([0-9]+)\s*$',text))
    return int(matches[-1].group(1)) if matches else None


def inspect(job_id=None):
    cmd=command(job_id)
    with connected() as s:
        if job_id is not None:
            start,end=date_range(31)
            rows=s.get('/api/job/getAllJobs',{'cluster':'training','startTime':start,'endTime':end}).json().get('results')
            if not isinstance(rows,list) or not any(r.get('jobId')==job_id for r in rows if isinstance(r,dict)):
                raise ServiceError('最近31天的本人作业中没有这个编号，请先核对作业记录。',code='invalid_arguments')
        s.goto('/loginCluster')
        with s.context.expect_page(timeout=15000) as opened:
            s.page.get_by_role('button',name='打 开',exact=True).click()
        page=opened.value
        try:
            page.wait_for_load_state('domcontentloaded')
            url=shell_url(page.url)
            page.locator('.xterm-helper-textarea').wait_for(timeout=15000)
            page.wait_for_function('(account)=>document.body.innerText.includes("以ID: "+account+" 连接到集群")',arg=s.account.lower(),timeout=15000)
            page.wait_for_function('()=>document.querySelector(".xterm-screen")?.innerText.includes("$ ")',timeout=15000)
            marker='__SCHOOL_DONE_'+uuid.uuid4().hex
            # GNU timeout owns the fixed read-command group and kills it after the grace period.
            line='timeout --kill-after=2s 15s bash -lc '+shlex.quote(cmd)+'; printf "\\n'+marker+':%s\\n" "$?"'
            field=page.locator('.xterm-helper-textarea');field.focus();field.fill(line);field.press('Enter')
            text='';code=None
            for _ in range(100):
                text=page.locator('.xterm-screen').inner_text();code=completed(text,marker)
                if code is not None:break
                page.wait_for_timeout(200)
            if code is None:
                raise ServiceError('终端未返回完整结束标记；固定查询已设置15秒超时，请重新查看平台记录。',code='result_unknown')
            # The prompt's echoed command may wrap. Keep only output after its marker format.
            lines=text.splitlines();end=next(i for i,v in enumerate(lines) if re.fullmatch(re.escape(marker)+r':\d+\s*',v))
            account_lines=[i for i,v in enumerate(lines[:end]) if v.strip().casefold()==s.account.casefold()]
            if not account_lines:
                raise ServiceError('终端执行账号未能核实。',code='identity_mismatch')
            start=max(0,account_lines[-1]-1)
            output='\n'.join(lines[start:end]).strip()
            found=bool(re.search(r'(?m)^\s*'+str(job_id)+r'\s',output)) if job_id else None
            return {'account':s.account,'command':cmd,'output':output,'exit_code':code,'succeeded':code==0,'job_found':found,
                    'history_note':'终端记账未返回该作业；请用作业查询工具核对107网页历史记录。' if found is False else '',
                    'source_url':url,'login_url':BASE+'/loginCluster','job_id':job_id,
                    'scope':'本人官方终端中的固定只读查询；不提交或取消计算作业。'}
        finally:
            try:
                page.locator('.xterm-helper-textarea').press('Control+c',timeout=1000)
                page.locator('.xterm-helper-textarea').fill('exit',timeout=1000)
                page.locator('.xterm-helper-textarea').press('Enter',timeout=1000)
            except Exception:pass
            page.close()
