"""Frozen experiment packages and durable, single-submit Slurm jobs."""
import base64
import hashlib
import io
import json
import re
import shlex
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath
import httpx
from . import transport
from ..operation_records import Records, stamp
from ..mail.config import local_dir
from ..service_errors import ServiceError

LIMIT = 60*1024*1024
PYTHON = '/public/app/python3.12/bin/python3'


def validate_archive(raw, entrypoint):
    if len(raw)>LIMIT: raise ValueError('实验ZIP不能超过60MB；大型模型请在计算节点按来源下载。')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names=set(); total=0
        if len(z.infolist())>2000: raise ValueError('实验文件数量超过2000。')
        for info in z.infolist():
            name=info.filename; p=PurePosixPath(name)
            if not name or p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name or '\x00' in name or stat.S_ISLNK(info.external_attr>>16):
                raise ValueError('实验包包含不安全路径或符号链接。')
            if name in names: raise ValueError('实验包有重复路径。')
            names.add(name); total+=info.file_size
            if total>200*1024*1024 or info.flag_bits&1: raise ValueError('实验包展开超过200MB或含加密文件。')
        if entrypoint not in names or not entrypoint.endswith('.py') or entrypoint.endswith('/'):
            raise ValueError('entrypoint必须是ZIP内的Python脚本路径。')
        if z.testzip(): raise ValueError('实验ZIP损坏。')
        return {'files': sorted(names), 'unpacked_bytes': total, 'setup': 'setup.sh' in names}


def prepare(files, entrypoint, arguments=None, cpus=2, memory_gb=8, gpus=0, minutes=10):
    arguments=arguments or []
    if not isinstance(files,list) or len(files)!=1: raise ValueError('请提供一个已选择的实验ZIP。')
    if not isinstance(entrypoint,str) or len(entrypoint)>200 or any(type(x) is not int for x in (cpus,memory_gb,gpus,minutes)) or not (1<=cpus<=4 and 1<=memory_gb<=16 and gpus in (0,1) and 1<=minutes<=240):
        raise ValueError('资源限1–4核、1–16GB、0–1GPU和1–240分钟。')
    if not isinstance(arguments,list) or len(arguments)>40 or any(not isinstance(x,str) or len(x)>1000 or '\x00' in x for x in arguments): raise ValueError('启动参数无效。')
    path=Path(files[0]);
    if not path.is_absolute() or not path.is_file() or path.is_symlink() or path.stat().st_size>LIMIT: raise ValueError('实验ZIP路径无效或过大。')
    raw=path.read_bytes(); manifest=validate_archive(raw,entrypoint)
    spec={'entrypoint':entrypoint,'arguments':arguments,'cpus':cpus,'memory_gb':memory_gb,'gpus':gpus,'minutes':minutes,'python':PYTHON,'environment':'isolated venv with shared runtime','partition':'Students','slurm_account':'stu','qos':'qos_stu_default'}
    sha=hashlib.sha256(raw).hexdigest(); store=Records('compute')
    v=store.create({'state':'prepared','phase':'prepared','archive_sha256':sha,'spec':spec,'manifest':manifest,'job_id':None})
    folder=local_dir()/'compute-packages'; folder.mkdir(exist_ok=True)
    target=folder/(v['id']+'.zip')
    with target.open('xb') as f: f.write(raw)
    v=store.update(v['id'],package_path=str(target.resolve()))
    return view(v)


def view(v):
    return {'experiment_id':v['id'], **{k:v[k] for k in ('state','phase','archive_sha256','spec','manifest','job_id','updated_at','preflight','scheduler','receipt','log_tail','error','artifact') if k in v},
            'source_url':'https://107.ustc.edu.cn/jobs/allJobs','status_tool':'school_compute_experiment_status',
            'can_submit':v['state']=='prepared' and not v.get('submit_claim'), 'content_is_untrusted':True}


def preflight(experiment_id):
    store=Records('compute'); v=store.read(experiment_id)
    from .client import check
    check()
    result=transport.execute(transport.script('''
def run(args):
 p=subprocess.run(args,capture_output=True,text=True,timeout=12)
 return {'exit_code':p.returncode,'output':p.stdout[-10000:]}
who=os.environ.get('USER','')
assert who.casefold()==v['account'].casefold()
python=run([v['python'],'-c','import sys,json,importlib.util;print(json.dumps({"python":sys.version,"torch":bool(importlib.util.find_spec("torch")),"transformers":bool(importlib.util.find_spec("transformers"))}))'])
association=run(['sacctmgr','-nP','show','assoc','where','user='+who,'format=User,Account,Partition,QOS'])
qos=run(['sacctmgr','-nP','show','qos',v['qos'],'format=Name,MaxWall,MaxTRES,MaxTRESPU'])
queue=run(['squeue','-u',who,'-h','-o','%i|%j|%T|%R'])
emit({'account':who,'python':python,'association':association,'qos':qos,'queue':queue})
''',{'account':store.account,**v['spec']}),60)
    rows=result['association']['output'].splitlines()
    eligible=any((len(p:=line.split('|'))>=4 and p[0].casefold()==store.account and p[1]=='stu' and p[2].casefold() in ('','students') and 'qos_stu_default' in p[3].split(',')) for line in rows)
    result['eligible']=eligible and result['python']['exit_code']==0
    result['checked_at']=stamp()
    return view(store.update(experiment_id,preflight=result))


def remote_dir(identifier):
    if not re.fullmatch('[a-f0-9]{32}',identifier): raise ValueError('实验编号无效。')
    return 'school-agent-experiments/'+identifier


def job_script(v):
    s=v['spec']; command=[s['python'],s['entrypoint'],*s['arguments']]
    # Setup and Python run only in the allocated Slurm job, never on login node.
    runner='''import json,os,pathlib,subprocess,sys,time,traceback,importlib.metadata
root=pathlib.Path(__file__).resolve().parent
receipt={'job_id':os.environ.get('SLURM_JOB_ID'),'started_at':time.time(),'node':os.uname().nodename}
try:
 os.chdir(root/'work'); os.environ['SCHOOL_OUTPUT_DIR']=str(root/'results'); os.environ['PYTHONUNBUFFERED']='1'
 (root/'phase').write_text('environment')
 env=root/'env';subprocess.run([sys.executable,'-m','venv','--without-pip',str(env)],check=True)
 site=env/'lib'/('python'+str(sys.version_info.major)+'.'+str(sys.version_info.minor))/'site-packages'
 site.mkdir(parents=True,exist_ok=True);(site/'shared_runtime.pth').write_text(str(pathlib.Path(sys.prefix)/'lib'/('python'+str(sys.version_info.major)+'.'+str(sys.version_info.minor))/'site-packages'))
 os.environ['PATH']=str(env/'bin')+os.pathsep+os.environ['PATH'];os.environ['VIRTUAL_ENV']=str(env);os.environ['SCHOOL_PYTHON']=str(env/'bin/python')
 if pathlib.Path('setup.sh').is_file():
  (root/'phase').write_text('environment')
  subprocess.run(['bash','setup.sh'],check=True)
 (root/'phase').write_text('running')
 command=COMMAND;command[0]=str(env/'bin/python')
 receipt['exit_code']=subprocess.run(command).returncode
except BaseException:
 receipt['exit_code']=1; traceback.print_exc()
finally:
 receipt['finished_at']=time.time()
 temporary=root/'receipt.tmp'; temporary.write_text(json.dumps(receipt)); temporary.replace(root/'receipt.json')
 (root/'phase').write_text('completed' if receipt['exit_code']==0 else 'failed')
sys.exit(receipt['exit_code'])
'''.replace('COMMAND',repr(command))
    batch='#!/bin/bash\n#SBATCH --partition=Students\n#SBATCH --account=stu\n#SBATCH --qos=qos_stu_default\n#SBATCH --cpus-per-task='+str(s['cpus'])+'\n#SBATCH --mem='+str(s['memory_gb'])+'G\n#SBATCH --time='+str(s['minutes'])+':00\n'
    if s['gpus']: batch+='#SBATCH --gres=gpu:1\n'
    batch+='set -euo pipefail\nexec '+shlex.quote(PYTHON)+' runner.py\n'
    return runner,batch


def submit(experiment_id, expected_sha256, authorized=False):
    if authorized is not True: return {'state':'authorization_required','experiment_id':experiment_id}
    store=Records('compute'); v=store.read(experiment_id)
    if expected_sha256!=v['archive_sha256']: raise ValueError('实验包摘要与预览不一致。')
    if v.get('submit_claim'): return view(v)
    checked=preflight(experiment_id)
    if not checked['preflight']['eligible']: return dict(checked,state='preflight_failed')
    raw=Path(v['package_path']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected_sha256: raise ValueError('实验包已变化。')
    runner,batch=job_script(v)
    v=store.claim(experiment_id,'submit_claim')
    if v is None: return view(store.read(experiment_id))
    store.update(experiment_id,state='submitting',phase='uploading')
    try:
        path=remote_dir(experiment_id)
        # Exclusive remote directory persists even if the local receipt is lost.
        transport.execute(transport.script("p=pathlib.Path.home()/v['path'];p.mkdir(parents=True,exist_ok=False);(p/'upload.zip').touch();emit({'created':True})",{'path':path}))
        for offset in range(0,len(raw),192*1024):
            block=raw[offset:offset+192*1024]
            transport.execute(transport.script("p=pathlib.Path.home()/v['path']/'upload.zip';assert p.stat().st_size==v['offset'];f=p.open('ab');f.write(base64.b64decode(v['block']));f.close();emit({'size':p.stat().st_size})",{'path':path,'offset':offset,'block':base64.b64encode(block).decode()}))
        result=transport.execute(transport.script('''
import zipfile
p=pathlib.Path.home()/v['path']; raw=(p/'upload.zip').read_bytes();assert hashlib.sha256(raw).hexdigest()==v['sha']
(p/'work').mkdir();(p/'results').mkdir()
with zipfile.ZipFile(p/'upload.zip') as z: z.extractall(p/'work')
(p/'runner.py').write_text(v['runner']);(p/'job.sh').write_text(v['batch']);(p/'phase').write_text('queued')
(p/'submit.started').touch(exist_ok=False)
q=subprocess.run(['sbatch','--parsable','--job-name=sa-'+v['id'],'--chdir='+str(p),'--output='+str(p/'slurm.log'),'--error='+str(p/'slurm.log'),str(p/'job.sh')],capture_output=True,text=True,timeout=20)
if q.returncode:
 (p/'submit.failed').write_text(q.stderr[:4000]);emit({'accepted':False,'error':'scheduler_rejected'})
else:
 job=int(q.stdout.strip().split(';')[0]);(p/'job-id').write_text(str(job));emit({'accepted':True,'job_id':job})
''',{'path':path,'sha':expected_sha256,'runner':runner,'batch':batch,'id':experiment_id}),40)
        return view(store.update(experiment_id,state='submitted' if result['accepted'] else 'failed',phase='queued' if result['accepted'] else 'submit_failed',job_id=result.get('job_id'),error=result.get('error')))
    except Exception:
        store.update(experiment_id,state='unknown',phase='submission_receipt_missing')
        raise


def status(experiment_id, refresh=True):
    store=Records('compute'); v=store.read(experiment_id)
    if not refresh or not v.get('submit_claim'): return view(v)
    result=transport.execute(transport.script('''
p=pathlib.Path.home()/v['path'];who=os.environ.get('USER','');assert who.casefold()==v['account'].casefold()
result={'phase':(p/'phase').read_text() if (p/'phase').is_file() else 'unknown'}
job=int((p/'job-id').read_text()) if (p/'job-id').is_file() else v['job_id']
if not job:
 q=subprocess.run(['squeue','-u',who,'-h','-n','sa-'+v['id'],'-o','%i'],capture_output=True,text=True,timeout=12)
 ids=[int(x) for x in q.stdout.split() if x.isdigit()]
 if len(ids)==1: job=ids[0];(p/'job-id').write_text(str(job))
result['job_id']=job
if job:
 q=subprocess.run(['scontrol','show','job',str(job),'-o'],capture_output=True,text=True,timeout=12)
 info=dict(x.split('=',1) for x in q.stdout.split() if '=' in x)
 if info:
  assert info.get('UserId','').split('(')[0].casefold()==who.casefold() and info.get('JobName')=='sa-'+v['id']
  result['scheduler']={k:info.get(k) for k in ('JobId','JobState','RunTime','TimeLimit','Reason','ExitCode','NodeList')}
if (p/'receipt.json').is_file():result['receipt']=json.loads((p/'receipt.json').read_text())
if (p/'slurm.log').is_file():
 with (p/'slurm.log').open('rb') as f:f.seek(max(0,(p/'slurm.log').stat().st_size-8000));result['log_tail']=f.read().decode('utf8','replace')
if (p/'submit.failed').is_file():result['submit_failed']=True
emit(result)
''',{'path':remote_dir(experiment_id),'id':experiment_id,'account':store.account,'job_id':v['job_id']}),40)
    # A missing fresh scheduler response must not leave yesterday's state visible.
    result.setdefault('scheduler', {})
    result.setdefault('receipt', None)
    if result.get('receipt') and str(result['receipt'].get('job_id')) != str(result.get('job_id')):
        raise ServiceError('实验回执与调度任务编号不一致。', code='result_unknown')
    sched=result.get('scheduler',{}).get('JobState','')
    if result.get('receipt'):
        state='completed' if result['receipt'].get('exit_code')==0 else 'failed'
    elif sched in ('CANCELLED','TIMEOUT','FAILED','OUT_OF_MEMORY','NODE_FAIL'): state=sched.lower()
    elif sched=='PENDING': state='queued'
    elif sched in ('RUNNING','COMPLETING'): state='running'
    elif result.get('submit_failed'): state='failed'
    else: state='unknown'
    result['state']=state
    return view(store.update(experiment_id,**result))


def cancel(experiment_id, authorized=False):
    if authorized is not True: return {'state':'authorization_required','experiment_id':experiment_id}
    store=Records('compute'); v=store.read(experiment_id)
    current=status(experiment_id)
    if current['state'] in ('completed','failed','cancelled','timeout','out_of_memory','node_fail'): return current
    job=current.get('job_id')
    if not job: return dict(current,cancelled=False)
    result=transport.execute(transport.script('''
q=subprocess.run(['scontrol','show','job',str(v['job']),'-o'],capture_output=True,text=True,timeout=12)
info=dict(x.split('=',1) for x in q.stdout.split() if '=' in x)
assert info.get('UserId','').split('(')[0].casefold()==v['account'].casefold() and info.get('JobName')=='sa-'+v['id']
q=subprocess.run(['scancel',str(v['job'])],capture_output=True,text=True,timeout=12)
emit({'requested':q.returncode==0})
''',{'job':job,'id':experiment_id,'account':store.account}),35)
    store.update(experiment_id,cancel_requested=result['requested'])
    return dict(status(experiment_id),cancel_requested=result['requested'])


def collect(experiment_id):
    store=Records('compute'); v=store.read(experiment_id)
    current=status(experiment_id)
    if current['state'] not in ('completed','failed','cancelled','timeout','out_of_memory','node_fail'):
        return dict(current,collection_ready=False)
    result=transport.execute(transport.script('''
import zipfile
p=pathlib.Path.home()/v['path'];target=p/'artifacts.zip';total=0;files=[]
for candidate in list((p/'results').rglob('*'))+[p/'receipt.json',p/'slurm.log',p/'phase']:
 if candidate.is_symlink():raise ValueError('symlink output')
 if candidate.is_file():
  assert candidate.resolve().is_relative_to(p.resolve());total+=candidate.stat().st_size
  if total>v['limit'] or len(files)>=2000:raise ValueError('artifact limit')
  files.append(candidate)
with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
 for f in files:z.write(f,f.relative_to(p).as_posix())
sha=hashlib.sha256(target.read_bytes()).hexdigest();emit({'path':str(target),'sha256':sha,'bytes':target.stat().st_size,'files':len(files)})
''',{'path':remote_dir(experiment_id),'limit':LIMIT}),60)
    if result['bytes']>LIMIT: raise ValueError('结果包超过60MB，请缩小results目录。')
    account, jar=transport.cookies()
    if account.casefold()!=store.account: raise ValueError('账号已变化。')
    folder=local_dir()/'compute-downloads';folder.mkdir(exist_ok=True)
    target=folder/(experiment_id+'.zip'); temp=folder/(experiment_id+'-'+uuid.uuid4().hex+'.part')
    try:
        sha=hashlib.sha256();size=0
        with httpx.Client(cookies=jar,trust_env=False,timeout=httpx.Timeout(60,connect=20),follow_redirects=False) as c:
            with c.stream('GET','https://107.ustc.edu.cn/api/file/download',params={'path':result['path'],'cluster':'training','download':'true'}) as response:
                response.raise_for_status()
                with temp.open('wb') as f:
                    for block in response.iter_bytes():
                        size+=len(block)
                        if size>LIMIT:raise ValueError('结果包超过上限。')
                        sha.update(block);f.write(block)
        if size!=result['bytes'] or sha.hexdigest()!=result['sha256']: raise ValueError('结果包摘要不一致。')
        with zipfile.ZipFile(temp) as z:
            if z.testzip():raise ValueError('结果包损坏。')
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    artifact={'path':str(target.resolve()),'filename':'107实验结果_'+experiment_id[:8]+'.zip','sha256':result['sha256'],'size_bytes':size}
    store.update(experiment_id,artifact=artifact)
    return dict(current,**artifact,collection_ready=True)
