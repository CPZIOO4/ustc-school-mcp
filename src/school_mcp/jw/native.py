"""Temporary IO bridge; parsing and planning are performed by the Rust core."""
from ..desktop import native_core
from ..desktop.model import ModelError
from .session import JWError


def enabled(client=None):
    return native_core.enabled() and (client is None or getattr(client, 'transport', None) is None)


def call(action, **arguments):
    try:
        return native_core.call('jw', {'action': action, **arguments})
    except ModelError as exc:
        raise JWError(str(exc), code=exc.code) from None


def body(action, response, **arguments):
    return call(action, text=response.text, url=str(response.url), **arguments)
