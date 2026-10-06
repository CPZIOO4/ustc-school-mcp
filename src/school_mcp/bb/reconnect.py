from __future__ import annotations

from typing import Any
from .identity import status
from .session import BBError
from ..login_runtime import running as _running, progress, start_login


def auth_status() -> dict[str, Any]:
    return {**status(), **progress("bb")}


def start(force_identity_login: bool = False) -> dict[str, Any]:
    from .login import login_state
    return start_login("bb", login_state, BBError, force_identity_login)
