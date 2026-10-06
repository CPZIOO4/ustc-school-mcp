from __future__ import annotations


from ..bb.identity import status as identity_status
from ..login_runtime import progress, start_login
from .session import JWError, load_session


def status() -> dict:
    auth = identity_status()
    result = {"configured": False, "credentials_saved": auth["credentials_saved"], "device_state_saved": auth["device_state_saved"], "email_verification_enabled": auth["email_verification_enabled"], "read_only": True}
    try:
        session = load_session()
        result.update(configured=True, saved_at=session["saved_at"])
    except JWError as exc:
        result["next_step"] = str(exc)
    result.update(progress("jw"))
    return result


def start() -> dict:
    from .login import login_state
    return start_login("jw", login_state, JWError)
