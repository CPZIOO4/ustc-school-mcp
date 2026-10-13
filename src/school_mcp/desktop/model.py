"""Small Chat Completions compatible client. Credentials never enter model messages."""
from urllib.parse import urlsplit
import json
import re
import math
import secrets
import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .storage import read_private, write_private
from . import native_core

PROVIDERS = [
    {'id': 'deepseek', 'label': 'DeepSeek', 'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash'},
    {'id': 'ustc', 'label': '科大大模型公共服务', 'base_url': 'https://api.llm.ustc.edu.cn/v1', 'model': 'deepseek-v4-flash'},
    {'id': 'custom', 'label': '自定义兼容 API', 'base_url': '', 'model': ''},
]


class ModelError(ValueError):
    """Stable user-facing errors, without provider response bodies or credentials."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def request_payload(model, messages, tools, force_tool=None):
    payload = dict(model=model, messages=messages, stream=False)
    if tools:
        payload.update(tools=tools, tool_choice={'type': 'function', 'function': {'name': force_tool}}
                       if force_tool else 'auto')
    return json.dumps(payload, ensure_ascii=False).encode('utf-8')


def tool_arguments(raw):
    if native_core.enabled():
        return native_core.call('tool_arguments', raw)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('non-finite number')
    def number(raw):
        result = float(raw)
        if not math.isfinite(result):
            raise ValueError('non-finite number')
        return result
    try:
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > 128 * 1024:
            raise ValueError('size or type')
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
        if not isinstance(value, dict):
            raise ValueError('not an object')
        return value
    except (ValueError, TypeError, RecursionError):
        raise ModelError('invalid_tool_arguments', '模型工具参数无效；本轮未执行这些调用，请重新描述需求。') from None


def validated_message(value):
    """Validate the whole batch before persisting it or dispatching its first call."""
    if native_core.enabled():
        return native_core.call('message_validate', value)
    try:
        if not isinstance(value, dict) or value.get('role') != 'assistant':
            raise ValueError()
        if value.get('refusal'):
            raise ModelError('model_refusal', '模型拒绝了本次请求；未执行新的工具调用。')
        for key in ('content', 'reasoning_content'):
            if value.get(key) is not None and not isinstance(value[key], str):
                raise ValueError()
        calls = value.get('tool_calls')
        if calls is not None and (not isinstance(calls, list) or len(calls) > 6):
            raise ValueError()
        if not calls and not (value.get('content') or '').strip():
            raise ModelError('empty_response', '模型返回了空结果；本轮已停止。')
        seen = set()
        clean_calls = []
        for call in calls or []:
            if not isinstance(call, dict) or call.get('type') != 'function':
                raise ValueError()
            identifier = call.get('id')
            fn = call.get('function')
            if (not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', identifier)
                    or identifier in seen or not isinstance(fn, dict)
                    or not isinstance(fn.get('name'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', fn['name'])):
                raise ValueError()
            seen.add(identifier)
            tool_arguments(fn.get('arguments'))
            clean_calls.append({'id': identifier, 'type': 'function', 'function': {'name': fn['name'], 'arguments': fn['arguments']}})
        result = {k: value[k] for k in ('role', 'content', 'reasoning_content') if k in value}
        if clean_calls:
            result['tool_calls'] = clean_calls
        return result
    except ModelError:
        raise
    except (ValueError, TypeError, KeyError):
        raise ModelError('invalid_model_response', '模型响应结构不完整；本轮未执行这些调用，请检查模型兼容性。') from None


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=160)
    api_key: str = Field(min_length=1, max_length=4096)

    @field_validator('base_url')
    @classmethod
    def url(cls, value):
        value = value.strip().rstrip('/')
        p = urlsplit(value)
        if p.port is not None and not 1 <= p.port <= 65535:
            raise ValueError('API 端口无效。')
        if (not p.hostname or p.username or p.password or p.query or p.fragment
                or p.scheme not in {'https', 'http'}
                or p.scheme == 'http' and p.hostname not in {'127.0.0.1', 'localhost', '::1'}):
            raise ValueError('请填写 HTTPS API 基础地址；本机模型可使用 localhost 的 HTTP 地址。')
        if p.path.endswith('/chat/completions'):
            value = value[:-len('/chat/completions')]
        return value

    @field_validator('model', 'api_key')
    @classmethod
    def text(cls, value):
        if any(c in value for c in '\r\n\x00') or not value.strip():
            raise ValueError('模型名和密钥不可为空或包含换行。')
        return value.strip()


def public_settings(data):
    value = read_private(data.settings_path, {})
    return dict(base_url=value.get('base_url', ''), model=value.get('model', ''),
                configured=bool(value.get('api_key')), tested=value.get('tested', False))


def save_settings(data, value):
    config = resolve_settings(data, value)
    write_private(data.settings_path, {**config, 'tested': False})
    return public_settings(data)


def resolve_settings(data, value):
    old = read_private(data.settings_path, {})
    if not value.get('api_key'):
        if ModelSettings.url(value.get('base_url', '')) != old.get('base_url'):
            raise ValueError('更换 API 地址时请重新填写密钥。')
        value = {**value, 'api_key': old.get('api_key', '')}
    if native_core.enabled():
        return native_core.call('model_validate', value)
    return ModelSettings.model_validate(value).model_dump()


def http_failure(status):
    raise ModelError({400: 'model_incompatible', 401: 'model_authentication', 402: 'model_balance',
                      403: 'model_forbidden', 429: 'model_rate_limited', 404: 'model_not_found'}.get(status, 'model_http_error'),
        {400: '模型接口未接受请求格式，请选择支持工具调用的兼容模型。',
         401: 'API 密钥未被接受，请在模型设置中更换。', 402: '模型账户余额不足，请到服务商平台检查额度。',
         403: 'API 服务拒绝访问，请检查密钥权限。', 429: '模型服务限流或额度不足，请稍后再试。',
         404: 'API 地址或模型不存在，请检查地址或获取模型列表。'}.get(status, f'模型服务返回 HTTP {status}；本轮已停止。'))


def list_models(data, value, transport=None):
    config = resolve_settings(data, {**value, 'model': value.get('model') or 'model-discovery'})
    if native_core.enabled() and transport is None:
        return native_core.call('model_list', {'settings': config})
    try:
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False, transport=transport) as client:
            with client.stream('GET', config['base_url'] + '/models', headers={'Authorization': 'Bearer ' + config['api_key']}) as response:
                if response.status_code in {404, 405}:
                    return {'models': [], 'available': False, 'note': '此服务没有模型列表，请填写服务商提供的模型名，再保存并测试。'}
                if response.status_code != 200:
                    http_failure(response.status_code)
                parts, count = [], 0
                for part in response.iter_bytes():
                    count += len(part)
                    if count > 1024 * 1024:
                        raise ModelError('model_list_invalid', '模型列表过大，请手动填写模型名。')
                    parts.append(part)
        models = json.loads(b''.join(parts))['data']
        if not isinstance(models, list):
            raise ValueError()
        ids = list(dict.fromkeys(m['id'] for m in models if isinstance(m, dict)
            and isinstance(m.get('id'), str) and re.fullmatch(r'[^\s\x00-\x1f]{1,160}', m['id'])))[:200]
        return {'models': ids, 'available': bool(ids), 'note': '选择模型后保存并测试；列表不代表模型支持工具调用。'}
    except ModelError:
        raise
    except httpx.HTTPError:
        raise ModelError('model_connection', '暂时无法获取模型列表，可手动填写模型名后测试。') from None
    except (ValueError, KeyError, TypeError, RecursionError):
        raise ModelError('model_list_invalid', '服务未返回兼容的模型列表，可手动填写模型名后测试。') from None


class ModelClient:
    def __init__(self, settings, transport=None):
        if not settings or not settings.get('api_key'):
            raise ValueError('请先保存模型 API 配置。')
        self.settings = ModelSettings.model_validate({k: settings[k] for k in ModelSettings.model_fields})
        self.transport = transport

    def complete(self, messages, tools, *, force_tool=None):
        if native_core.enabled() and self.transport is None:
            return native_core.call('model_complete', {'settings': self.settings.model_dump(),
                'messages': messages, 'tools': tools, 'force_tool': force_tool})
        raw = request_payload(self.settings.model, messages, tools, force_tool)
        if len(raw) > 900_000:
            raise ModelError('context_capacity', '本轮必要上下文过大；原记录已保留，请缩小任务范围。')
        try:
            with httpx.Client(timeout=httpx.Timeout(120, connect=20), follow_redirects=False,
                              trust_env=False, transport=self.transport) as client:
                with client.stream('POST', self.settings.base_url + '/chat/completions',
                        headers={'Authorization': 'Bearer ' + self.settings.api_key, 'Content-Type': 'application/json'},
                        content=raw) as response:
                    if response.status_code != 200:
                        http_failure(response.status_code)
                    parts, count = [], 0
                    for part in response.iter_bytes():
                        count += len(part)
                        if count > 2 * 1024 * 1024:
                            raise ValueError('模型响应超过大小限制。')
                        parts.append(part)
            choice = json.loads(b''.join(parts))['choices'][0]
            if choice.get('finish_reason') in {'length', 'content_filter'}:
                raise ModelError('incomplete_model_response', '模型响应被截断或过滤；本轮未执行这些调用。')
            return validated_message(choice['message'])
        except httpx.TimeoutException:
            raise ModelError('model_timeout', '模型响应超时；不会自动重复请求。') from None
        except httpx.HTTPError:
            raise ModelError('model_connection', '无法连接模型 API，请检查地址及网络。') from None
        except (KeyError, IndexError, TypeError, AttributeError, RecursionError, UnicodeDecodeError, json.JSONDecodeError):
            raise ModelError('invalid_model_response', '模型响应不符合 Chat Completions 格式。') from None


def test_connection(data, notify=None):
    notify = notify or (lambda *_: None)
    settings = read_private(data.settings_path, {})
    if not settings.get('api_key'):
        raise ValueError('请先保存模型 API 配置。')
    # A failed recheck must not leave a historical green connection badge.
    settings = {**settings, 'tested': False}
    write_private(data.settings_path, settings)
    tool = {'type': 'function', 'function': {'name': 'connection_check', 'description': 'Return the connection check.',
             'parameters': {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}}}
    client = ModelClient(settings)
    messages = [{'role': 'user', 'content': 'Call connection_check with ok=true exactly once. After receiving its result, reply with only the verification_code from the tool result. This is a compatibility test without personal data.'}]
    notify('model_tool_check', '正在验证接口与工具调用…')
    result = validated_message(client.complete(messages, [tool]))
    calls = result.get('tool_calls', [])
    if (len(calls) != 1 or calls[0].get('function', {}).get('name') != 'connection_check'
            or tool_arguments(calls[0]['function']['arguments']) != {'ok': True}
            or type(tool_arguments(calls[0]['function']['arguments']).get('ok')) is not bool):
        raise ValueError('模型未完成工具调用测试；请检查模型是否支持 function calling。')
    code = secrets.token_hex(8)
    messages.extend([result, {'role': 'tool', 'tool_call_id': calls[0]['id'],
                             'content': json.dumps({'ok': True, 'verification_code': code})}])
    notify('model_result_check', '工具调用通过，正在验证结果读取…')
    answer = client.complete(messages, [tool])
    if answer.get('tool_calls') or code not in (answer.get('content') or ''):
        raise ModelError('model_tool_roundtrip', '模型能调用工具，但未正确读取工具结果；请更换模型后测试。')
    write_private(data.settings_path, {**settings, 'tested': True})
    notify('model_ready', '模型已就绪，可以开始对话。')
    return dict(connected=True, tool_calling=True, tool_result_read=True, school_data_sent=False)
