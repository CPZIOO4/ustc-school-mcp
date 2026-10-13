"""Temporary argument boundary: native Rust owns BB bytes, storage and submission IO."""
from ..desktop import native_core
from ..desktop.model import ModelError
from ..mail.config import local_dir
from .session import BBError, load_session


def call(action, *, account, session_binding=None, **values):
    directory = local_dir().absolute()
    request = {'action': action, 'account': account,
               'database': str(directory / 'bb-submissions.sqlite3'),
               'policy_database': str(directory / 'request-policy.sqlite3'), **values}
    if action != 'status' or values.get('verify'):
        request['state'] = load_session()
    if session_binding is not None:
        request['session_binding'] = session_binding
    try:
        return native_core.call('bb_io', request)
    except ModelError as exc:
        raise BBError(str(exc), code=exc.code) from None
