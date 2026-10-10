from __future__ import annotations

from datetime import datetime, timezone

from ..bb.identity import _load, _save, load_credentials
from ..bb.session import BBError

HOST = "young.ustc.edu.cn"
ORIGIN = "https://" + HOST
HOME = ORIGIN + "/login/sc-wisdom-group-learning/dataAnalysis/visual"


from ..service_errors import ServiceError


class YoungError(ServiceError):
    """Credential-free user-facing error."""


def own_state(state: dict) -> dict:
    return {"cookies": [c for c in state.get("cookies", []) if c.get("domain", "").lstrip(".") == HOST],
            "origins": [o for o in state.get("origins", []) if o.get("origin") == ORIGIN]}


def save_session(state: dict, account: str, user_agent: str | None) -> None:
    _save("young-session.dpapi", {"account": account, "state": own_state(state), "user_agent": user_agent,
                                 "saved_at": datetime.now(timezone.utc).isoformat()})


def load_session() -> dict:
    try:
        data = _load("young-session.dpapi")
        if data.get("account") != load_credentials()["username"]:
            raise YoungError("青春科大会话与当前统一身份账号不一致，请重新连接。", code='authentication_required')
        if not isinstance(data.get("state"), dict):
            raise ValueError()
        data["state"] = own_state(data["state"])
        return data
    except (BBError, ValueError, TypeError, KeyError):
        raise YoungError("青春科大尚未登录或本地会话无法读取，请调用 school_young_reconnect。", code='authentication_required') from None
