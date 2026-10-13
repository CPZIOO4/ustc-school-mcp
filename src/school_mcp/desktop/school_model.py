"""Reuse an existing school API key through official SSO, outside model context."""
import re
from ..campus.session import browser_session
from ..service_errors import ServiceError
from .model import save_settings, list_models, test_connection


def connect(s):
    page = s.goto()
    login = page.get_by_text('统一身份认证登录', exact=True)
    for _ in range(40):
        if login.count() and login.first.is_visible():
            login.first.click(timeout=10000)
            page.wait_for_timeout(800)
            break
        if page.get_by_text('API Key 管理', exact=True).count():
            break
        page.wait_for_timeout(100)
    s.require_site()
    user = s.get('/api/auth/me').json()
    s.confirm(user.get('login_username') or user.get('username'))
    return s.get('/api/api-keys').json()


def keys():
    with browser_session('llm') as s:
        values = connect(s)
        if not isinstance(values, list):
            raise ServiceError('学校平台未返回现有密钥列表。', code='unavailable')
        return {'keys': [{k: x.get(k) for k in ('id', 'name', 'status', 'models', 'expires_at')} for x in values],
                'note': '选择已有密钥后可在本机绑定。不会申请新项目或新额度；密钥不进入聊天。没有密钥时请先在学校模型平台申请。'}


def import_key(data, key_id, model, notify=None, stopped=None):
    if not isinstance(key_id, str) or not re.fullmatch(r'[a-fA-F0-9-]{36}', key_id):
        raise ValueError('请选择学校平台返回的密钥。')
    if not isinstance(model, str) or not model.strip() or len(model) > 160:
        raise ValueError('请选择模型。')
    with browser_session('llm') as s:
        values = connect(s)
        key = next((x for x in values if x.get('id') == key_id), None)
        if not key or key.get('status') != 'active':
            raise ValueError('当前账号没有这个密钥，请重新获取列表。')
        secret = s.get('/api/api-keys/' + key_id + '/secret').json().get('secret_key')
        if not isinstance(secret, str) or not secret:
            raise ValueError('学校平台没有返回可用密钥。')
        config = {'base_url': 'https://api.llm.ustc.edu.cn/v1', 'model': model, 'api_key': secret}
        available = list_models(data, config)['models']
        if model not in available:
            raise ValueError('该密钥没有所选模型，请选择其可用模型。')
        # Test separately first; a failed import preserves the current provider.
        from types import SimpleNamespace
        from .storage import write_private, read_private
        from . import native_core
        from .app_native import call
        candidate = SimpleNamespace(root=data.root, model_slot='candidate',
                                    settings_path=data.root / 'school-model-candidate.dpapi')
        result = None
        committed = False
        try:
            save_settings(candidate, config)
            result = test_connection(candidate, notify, stopped)
            if result.get('connected') is True:
                # A stop arriving after the compatibility reply must still stop
                # the next step: replacing the user's current provider.
                if stopped is not None and stopped.is_set():
                    result = {'state': 'stopped', 'connected': False, 'school_data_sent': False,
                              'note': '已停止模型导入；原模型配置保留。'}
                else:
                    if native_core.enabled():
                        call(candidate, 'commit_candidate')
                    else:
                        write_private(data.settings_path, read_private(candidate.settings_path))
                    committed = True
                    result = {**result, 'provider': 'ustc', 'model': model, 'existing_key_reused': True}
        finally:
            try:
                if native_core.enabled():
                    call(candidate, 'discard_candidate')
                else:
                    candidate.settings_path.unlink(missing_ok=True)
            except Exception as exc:
                # Cleanup failure must never replace the provider's original
                # error, or describe an already committed import as unexecuted.
                from ..safety_log import record
                incident = record(code='storage_failure', service='model', stage='persistence',
                                  outcome='local_failed', exc=exc)
                if isinstance(result, dict):
                    result.update(candidate_cleanup_pending=True, **incident)
                    if committed:
                        result['note'] = '模型已连接并保存；候选配置清理未完成，已记录诊断，无需重复导入。'
        return result
