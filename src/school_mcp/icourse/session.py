"""Independent iCourse session; never use the university password here."""
import json
from datetime import datetime, timezone
from ..mail.config import local_dir
from ..mail.credentials import _dpapi
from ..workflow_store import require, WorkflowError


def save_session(cookies, user_id, user_agent):
    require(str(user_id).isdigit(), '无法确认评课账号。')
    selected = [c for c in cookies if c.get('domain') in {'icourse.club','.icourse.club'}]
    require(selected, '未获得评课会话。')
    path = local_dir() / 'icourse-session.dpapi'
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_suffix('.dpapi.tmp')
    temporary.write_bytes(_dpapi(json.dumps(dict(cookies=selected,user_id=str(user_id),user_agent=user_agent,saved_at=datetime.now(timezone.utc).isoformat())).encode()))
    temporary.replace(path)


def load_session():
    try:
        data=json.loads(_dpapi((local_dir()/'icourse-session.dpapi').read_bytes(),decrypt=True))
        require(isinstance(data,dict) and str(data.get('user_id','')).isdigit() and isinstance(data.get('cookies'),list) and bool(data['cookies']), '评课会话格式异常。')
        require(all(c.get('domain') in {'icourse.club','.icourse.club'} for c in data['cookies']), '评课会话域名不符。')
        return data
    except Exception:
        raise WorkflowError('评课社区尚未配置独立登录；请在本机运行 python -m school_mcp icourse-login，不要在对话发送密码。') from None


def setup_guide():
    return dict(configured=(local_dir()/'icourse-session.dpapi').exists(), network_checked=False,
                next_action='run_local_login_if_needed', command='python -m school_mcp icourse-login',
                authentication='independent_icourse_account', browser='headless_chrome',
                note='本机终端遮罩输入评课社区自己的账号密码；密码不保存，仅保存加密会话。不自动注册、重置密码或借用学校统一身份密码。')
