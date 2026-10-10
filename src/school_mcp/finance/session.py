from datetime import datetime, timezone
from ..bb.identity import _load, _save, load_credentials
from ..bb.session import BBError

HOST = 'cwzh.ustc.edu.cn'
ORIGIN = 'https://' + HOST
HOME = ORIGIN + '/WFManager/home2.jsp'
SMART_HOME = ORIGIN + '/YBX/main.jsp'
PUBLIC_HOME = 'https://finance.ustc.edu.cn/'
GUIDE = 'https://finance.ustc.edu.cn/2025/0403/c21813a679402/page.psp'


from ..service_errors import ServiceError


class FinanceError(ServiceError):
    """Sanitized financial connector error."""


def own_state(state):
    return {'cookies': [c for c in state.get('cookies', []) if c.get('domain', '').lstrip('.') == HOST],
            'origins': [o for o in state.get('origins', []) if o.get('origin') == ORIGIN]}


def save_session(state, account, user_agent=None):
    _save('finance-session.dpapi', {'account': account, 'state': own_state(state), 'user_agent': user_agent,
                                   'saved_at': datetime.now(timezone.utc).isoformat()})


def load_session():
    try:
        value = _load('finance-session.dpapi')
        if value['account'] != load_credentials()['username'] or not isinstance(value['state'], dict):
            raise ValueError()
        value['state'] = own_state(value['state'])
        return value
    except (BBError, ValueError, TypeError, KeyError):
        raise FinanceError('财务会话未保存、无法读取或账号已改变，请调用 school_finance_reconnect。', code='authentication_required') from None
