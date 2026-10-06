from __future__ import annotations


from ..bb.identity import status as identity_status
from ..login_runtime import progress, start_login
from .session import LibraryError, load_session


def status() -> dict:
    auth = identity_status()
    result = {"configured": False, "credentials_saved": auth["credentials_saved"], "device_state_saved": auth["device_state_saved"], "email_verification_enabled": auth["email_verification_enabled"], "read_only": True}
    try:
        session = load_session()
        result.update(configured=True, saved_at=session["saved_at"], transport=session["base_url"].split(":", 1)[0])
    except LibraryError as exc:
        result["next_step"] = str(exc)
    result.update(progress("library"))
    return result


def start() -> dict:
    from .login import login_state
    return start_login("library", login_state, LibraryError)
