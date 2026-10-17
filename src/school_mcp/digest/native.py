"""Fixed Rust digest operations; a failed pipe never falls back or retries."""
from ..desktop import native_core

enabled = native_core.enabled


def directory(root):
    # Do not resolve symlinks before the Rust boundary checks their ancestors.
    return str(root.absolute())


def check_paths(root):
    return call('check_paths', root=directory(root))


def call(action, **values):
    from ..desktop.model import ModelError
    try:
        return native_core.call('digest', dict(action=action, **values))
    except ModelError as exc:
        # Keep the established digest API's validation/error boundary.
        raise ValueError(str(exc)) from None
