"""Machine-readable recovery reasons; never infer authentication from prose."""
from __future__ import annotations


class ServiceError(Exception):
    def __init__(self, message: str, *, code: str = 'unavailable', retry_after_seconds: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


def policy_error(error_type, error):
    result = error_type(str(error))
    result.retry_after_seconds = getattr(error, 'retry_after_seconds', None)
    result.code = 'cooldown' if result.retry_after_seconds else 'local_policy_unavailable'
    return result


def identity_redirect(url: str) -> bool:
    from urllib.parse import urlsplit
    try:
        value = urlsplit(url)
        return (value.scheme == 'https' and value.hostname in {'id.ustc.edu.cn', 'passport.ustc.edu.cn'}
                and value.port in (None, 443) and not value.username and not value.password)
    except ValueError:
        return False
