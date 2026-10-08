"""Windows-native Python worker scheduling. Task parameters contain no credentials."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

from ..mail.config import local_dir
from .registration import Jobs, load_session, require, stamp, YoungError


def valid_id(identifier):
    require(isinstance(identifier,str) and re.fullmatch('[a-f0-9]{32}',identifier),'schedule_id 无效。')
    return identifier


def spec_path(identifier):
    return local_dir()/'young-schedules'/f'{valid_id(identifier)}.json'


def read_spec(identifier):
    try:
        return json.loads(spec_path(identifier).read_text(encoding='utf-8'))
    except (OSError,ValueError):raise YoungError('本地脚本计划不存在或不可读。') from None


def write_spec(identifier,spec):
    path=spec_path(identifier);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(spec,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)


def powershell(script):
    require(os.name=='nt','本机自动触发暂仅支持 Windows 计划任务。')
    encoded=base64.b64encode(script.encode('utf-16le')).decode('ascii')
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],
        capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=30)
    require(result.returncode==0,'Windows 计划任务操作失败；请检查当前用户权限。未调用 AI 或回退到聊天调度。')
    try:return json.loads(result.stdout.decode('utf-8-sig'))
    except ValueError:raise YoungError('无法核验 Windows 计划任务结果。') from None


def task_name(identifier):return 'SchoolMCP-Young-'+valid_id(identifier)


def task_xml(identifier,monitor_from, *, executable=None,private_dir=None):
    exe=Path(executable or sys.executable).with_name('pythonw.exe')
    require(exe.exists(),'未找到无窗口 Python 解释器。')
    directory=str((private_dir or local_dir()).resolve())
    args=subprocess.list2cmdline(['-X','utf8','-m','school_mcp','young-registration-worker',
        '--schedule-id',valid_id(identifier),'--private-dir',directory])
    start=stamp(monitor_from).isoformat()
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<RegistrationInfo><Description>School MCP headless registration worker; no AI runtime.</Description></RegistrationInfo>
<Triggers><TimeTrigger><StartBoundary>{escape(start)}</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
<Principals><Principal id="Author"><UserId>__CURRENT_USER_SID__</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><Enabled>true</Enabled><Hidden>true</Hidden><WakeToRun>true</WakeToRun><ExecutionTimeLimit>PT0S</ExecutionTimeLimit></Settings>
<Actions Context="Author"><Exec><Command>{escape(str(exe.resolve()))}</Command><Arguments>{escape(args)}</Arguments><WorkingDirectory>{escape(str(Path(__file__).resolve().parents[3]))}</WorkingDirectory></Exec></Actions></Task>'''


def inspect_schedule(identifier):
    spec=read_spec(identifier)
    if spec.get('scheduler')=='background-python':
        from ..login_runtime import process_started_at, running
        pid=spec.get('pid',0);alive=running(pid) if pid else False
        started=process_started_at(pid) if alive else None
        alive=alive and started is not None and abs(started-spec.get('process_started_at',0))<0.1
        return dict(schedule_id=identifier,scheduler='background-python',enabled=alive and not spec.get('cancelled'),
            process_running=alive,job_id=spec.get('job_id'),worker_state=spec.get('worker_state'),monitor_from=spec['monitor_from'],
            opens_at=spec['opens_at'],uses_ai=False,survives_reboot=False)
    name=task_name(identifier)
    result=powershell(f"""$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new();
$t=Get-ScheduledTask -TaskName '{name}'; $i=$t | Get-ScheduledTaskInfo;
@{{task_name=$t.TaskName; state=[string]$t.State; enabled=[bool]$t.Settings.Enabled; start_boundary=$t.Triggers[0].StartBoundary; command=$t.Actions[0].Execute; arguments=$t.Actions[0].Arguments; last_result=$i.LastTaskResult}} | ConvertTo-Json -Compress""")
    return dict(schedule_id=identifier,scheduler='windows-task-scheduler',**result,job_id=spec.get('job_id'),worker_state=spec.get('worker_state'),monitor_from=spec['monitor_from'],opens_at=spec['opens_at'],uses_ai=False)


def install_schedule(monitor_from,opens_at, *, job_id=None,schedule_id=None):
    start=stamp(monitor_from);due=stamp(opens_at)
    require(start<=due and start>datetime.now(timezone.utc),'监控启动时间须在未来且不晚于报名开放时间。')
    identifier=valid_id(schedule_id) if schedule_id else uuid.uuid4().hex
    old=read_spec(identifier) if schedule_id else None
    require(old is None or not old.get('job_id') or old['job_id']==job_id,'不能将已有脚本计划改绑到另一个报名任务。')
    spec=dict(schedule_id=identifier,monitor_from=start.isoformat(),opens_at=due.isoformat(),job_id=job_id,
              worker_state='scheduled' if job_id else 'needs_exact_name',cancelled=False)
    if old and old.get('scheduler')=='background-python' and inspect_schedule(identifier)['process_running']:
        require(old['monitor_from']==spec['monitor_from'] and old['opens_at']==spec['opens_at'],'已运行的等待进程不能改动时刻；请先取消后重建。')
        write_spec(identifier,{**old,**spec})
        return inspect_schedule(identifier)
    write_spec(identifier,spec)
    xml=task_xml(identifier,start.isoformat())
    data=base64.b64encode(xml.encode('utf-8')).decode('ascii');name=task_name(identifier)
    registered=False
    try:
        powershell(f"""$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new();
$xml=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{data}'));
$xml=$xml.Replace('__CURRENT_USER_SID__',[Security.Principal.WindowsIdentity]::GetCurrent().User.Value);
Register-ScheduledTask -TaskName '{name}' -Xml $xml -Force | Out-Null;
@{{installed=$true}} | ConvertTo-Json -Compress""")
        registered=True
        installed=inspect_schedule(identifier)
        require(installed['enabled'] and stamp(installed['start_boundary'])==start,'计划任务时间或启用状态核验失败。')
        return installed
    except YoungError:
        if registered:
            powershell(f"$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); Disable-ScheduledTask -TaskName '{name}' | Out-Null; @{{disabled=$true}} | ConvertTo-Json -Compress")
        # No privilege escalation. Same deterministic worker waits in a detached
        # process when this user's Windows task service denies registration.
        return start_waiter(identifier,spec)


def start_waiter(identifier,spec):
    from ..login_runtime import process_started_at
    from ..background_process import spawn
    exe=Path(sys.executable).with_name('pythonw.exe')
    require(exe.exists(),'未找到无窗口 Python。')
    spec.update(scheduler='background-python',scheduler_warning='系统计划任务未成功核验，使用驻留Python等待；不跨电脑重启。')
    write_spec(identifier,spec)
    args=[str(exe),'-X','utf8','-m','school_mcp','young-registration-worker','--schedule-id',identifier,'--private-dir',str(local_dir())]
    env={**os.environ,'SCHOOL_MCP_LOCAL_DIR':str(local_dir()),'SCHOOL_MCP_BROWSER_HEADED':'0','PYTHONUTF8':'1'}
    with (spec_path(identifier).parent/f'{identifier}.log').open('ab') as log:
        try:
            process=spawn(args,stdout=log,stderr=log,env=env,cwd=Path(__file__).resolve().parents[3])
        except OSError:
            raise YoungError('系统不允许独立后台进程；未安排成功，不能保证启动器退出后继续运行。') from None
    spec.update(pid=process.pid,process_started_at=process_started_at(process.pid))
    write_spec(identifier,spec)
    result=inspect_schedule(identifier)
    require(result['process_running'],'后台等待进程未运行，不能视为已安排。')
    return result


def schedule_registration(exact_name,opens_at,authorized=False,item_id=None,allow_non_cancellable=False,
                          monitor_from=None,poll_seconds=10,stop_at=None,schedule_id=None,name_keywords=None):
    start=monitor_from or (stamp(opens_at)-timedelta(minutes=2)).isoformat()
    s=load_session()
    job=Jobs(s['account']).create(exact_name,opens_at,item_id=item_id,authorized=authorized,
        allow_non_cancellable=allow_non_cancellable,monitor_from=start,poll_seconds=poll_seconds,stop_at=stop_at,name_keywords=name_keywords)
    schedule=install_schedule(start,opens_at,job_id=job['job_id'],schedule_id=schedule_id)
    return dict(job=job,schedule=schedule,scheduler_installed=True,uses_ai=False)


def cancel_schedule(identifier):
    spec=read_spec(identifier)
    if spec.get('job_id'):
        store=Jobs(load_session()['account']);state=store.view(spec['job_id'])['state']
        if state in {'scheduled','monitoring','checking'}:store.cancel(spec['job_id'])
    spec['cancelled']=True;write_spec(identifier,spec)
    if spec.get('scheduler')=='background-python':return inspect_schedule(identifier)
    name=task_name(identifier)
    powershell(f"$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); Disable-ScheduledTask -TaskName '{name}' | Out-Null; @{{disabled=$true}} | ConvertTo-Json -Compress")
    return inspect_schedule(identifier)
