from ..bb.identity import status as identity_status
from ..login_runtime import progress, start_login
from .session import FinanceError, load_session


def status():
    auth = identity_status()
    result = {k: auth[k] for k in ('credentials_saved', 'device_state_saved', 'email_verification_enabled')}
    result.update(configured=False, read_only=True)
    try:
        session = load_session()
        result.update(configured=True, saved_at=session['saved_at'])
    except FinanceError as exc:
        result['next_step'] = str(exc)
    result.update(progress('finance'))
    return result


def start():
    from .login import login_state
    return start_login('finance', login_state, FinanceError)
