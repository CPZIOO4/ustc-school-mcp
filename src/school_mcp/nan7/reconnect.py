from __future__ import annotations


from ..bb.identity import status as identity_status
from ..login_runtime import progress, start_login
from .session import SITE_URL, Nan7Error, load_session


def status() -> dict:
    identity = identity_status()
    result = {"configured": False, "credentials_saved": identity["credentials_saved"], "device_state_saved": identity["device_state_saved"], "email_verification_enabled": identity["email_verification_enabled"], "read_only": True, "network_checked": False, "source": SITE_URL, "capabilities": ["sell_list", "buy_list", "search", "categories", "offer_detail"]}
    try:
        session = load_session()
        result.update(configured=True, saved_at=session["saved_at"])
    except Nan7Error as exc:
        result["next_step"] = str(exc)
    result.update(progress("nan7"))
    return result


def start() -> dict:
    from .login import login_state
    return start_login("nan7", login_state, Nan7Error)
