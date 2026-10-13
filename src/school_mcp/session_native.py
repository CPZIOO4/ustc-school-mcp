"""Private adapters for Rust's fixed-site session policy; no Python fallback on failure."""
from pathlib import Path
from .desktop import native_core


def enabled():
    return native_core.enabled()


def invoke(operation, profile, error_type, **values):
    from .desktop.model import ModelError
    if 'path' in values:
        values['path'] = str(Path(values['path']).resolve())
    try:
        return native_core.call(operation, {'profile': profile, **values})
    except ModelError as exc:
        message = str(exc)
        if operation == 'sessions_read' and exc.code in {'authentication_required', 'storage_failure'}:
            message = {
                'bb': 'BB 尚未登录或本地会话无法读取，请运行 BB 本地登录窗口。',
                'jw': '教务系统尚未登录或会话无法读取，请调用 school_jw_reconnect。',
                'library': '个人图书馆尚未登录或会话无法读取，请调用 school_library_reconnect。',
                'finance': '财务会话未保存或无法读取，请调用 school_finance_reconnect。',
                'young': '青春科大尚未登录或本地会话无法读取，请调用 school_young_reconnect。',
                'icourse': '评课社区尚未配置独立登录，请在本机完成官方登录。',
                'nan7': '南七集市尚未登录或会话无法读取，请调用 school_nan7_reconnect。',
            }.get(profile, message)
        error = error_type(message)
        error.code = exc.code
        raise error from None


def current_account():
    from .bb.identity import load_credentials
    from .bb.session import BBError
    try:
        return load_credentials()['username']
    except BBError as exc:
        if exc.code == 'authentication_required':
            return None
        raise
