"""Private fixed-operation SCOW transport; never a model-callable shell API."""
import base64
import json
import re
import textwrap
import time
import uuid
from websockets.sync.client import connect
from ..bb.identity import load_credentials
from ..campus.session import initial_state
from ..service_errors import ServiceError


def cookies():
    account = load_credentials()['username']
    state = initial_state('compute', account)
    return account, {c['name']: c['value'] for c in state['cookies'] if c.get('domain', '').lstrip('.') in ('107.ustc.edu.cn', 'ustc.edu.cn')}


def execute(code, timeout=45):
    account, jar = cookies()
    marker = 'SCHOOL_DONE_' + uuid.uuid4().hex
    ws = None
    strip = lambda t: re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', t).replace('\r', '')
    try:
        ws = connect('wss://107.ustc.edu.cn/api/shell?cluster=training&loginNode=11.11.10.202&cols=180&rows=40&path=',
                     additional_headers={'Cookie': '; '.join(k+'='+v for k,v in jar.items())},
                     origin='https://107.ustc.edu.cn', proxy=None, open_timeout=20, close_timeout=3, max_size=8*1024*1024)
        def send(s): ws.send(json.dumps({'$case': 'data', 'data': {'data': s}}))
        def receive(wait):
            msg = json.loads(ws.recv(timeout=wait)); value = msg.get('data', {}).get('data', '')
            return bytes(value['data']).decode('utf8', 'replace') if isinstance(value, dict) else value
        text = ''; deadline = time.monotonic()+20
        while time.monotonic()<deadline:
            try: text += receive(min(5, max(.1, deadline-time.monotonic())))
            except TimeoutError: continue
            if re.search(r'[$#] ?$', strip(text).rstrip()): break
        if account.casefold() not in text.casefold():
            raise ServiceError('107终端身份尚未确认，请恢复统一身份连接。', code='authentication_required')
        send("stty -echo; export PS2=''\r"); time.sleep(.15)
        encoded = base64.b64encode(code.encode()).decode(); delimiter = 'PY_'+uuid.uuid4().hex
        script = 'timeout --kill-after=3s '+str(int(timeout))+"s python3 - <<'"+delimiter+"'\nimport base64\nexec(base64.b64decode('''\n"+'\n'.join(textwrap.wrap(encoded, 1000))+"\n'''))\n"+delimiter+'\nprintf "\\n'+marker+':%s\\n" "$?"\n'
        for i in range(0, len(script), 12000): send(script[i:i+12000])
        text = ''; deadline = time.monotonic()+timeout+8
        while time.monotonic()<deadline:
            try: text += receive(min(5, max(.1, deadline-time.monotonic())))
            except TimeoutError: continue
            if len(text)>8*1024*1024: raise ServiceError('终端结果超过上限。', code='result_unknown')
            clean = strip(text); m = re.search(r'(?m)^'+marker+r':(\d+)\s*$', clean)
            if m:
                if int(m[1]): raise ServiceError('107操作未成功，请核实原任务及日志。', code='upstream_error')
                clean = re.sub(r'\x1b\][^\x07]*\x07', '', clean[:m.start()])
                rows = [line[len('SCHOOL_JSON:'):] for line in clean.splitlines() if line.startswith('SCHOOL_JSON:')]
                if not rows: raise ServiceError('107未返回完整业务回执。', code='result_unknown')
                result = json.loads(rows[-1])
                if load_credentials()['username'].casefold()!=account.casefold(): raise ServiceError('账号已变化。', code='identity_mismatch')
                return result
        raise ServiceError('107操作回执未完整返回，请查询原实验状态。', code='result_unknown')
    finally:
        if ws:
            try:
                ws.send(json.dumps({'$case':'data','data':{'data':'\x03exit\r'}}))
                ws.send(json.dumps({'$case':'disconnect'}))
            except Exception: pass
            ws.close()


def script(body, values):
    encoded = base64.b64encode(json.dumps(values, ensure_ascii=False).encode()).decode()
    return "import base64,json,os,pathlib,subprocess,hashlib,time\nv=json.loads(base64.b64decode("+repr(encoded)+"))\ndef emit(x): print('SCHOOL_JSON:'+json.dumps(x,ensure_ascii=False),flush=True)\n"+body
