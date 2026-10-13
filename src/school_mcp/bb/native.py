"""Temporary IO boundary for Blackboard's Rust business contracts."""
from ..desktop import native_core
from ..desktop.model import ModelError
from .session import BBError


def enabled():
    return native_core.enabled()


def call(action, **values):
    # Attachment bytes stay in the IO layer; Rust receives only frozen metadata.
    # This also keeps 50 MiB preparation snapshots below the pipe frame bound.
    def plan_view(plan):
        return {**plan, 'files': [{k: value for k, value in item.items() if k != 'data'}
                                 for item in plan['files']]}
    if 'plan' in values:
        values['plan'] = plan_view(values['plan'])
    if 'job' in values:
        values['job'] = {**values['job'], 'plan': plan_view(values['job']['plan'])}
    try:
        return native_core.call('bb_adapter', {'action': action, **values})
    except ModelError as exc:
        raise BBError(str(exc), code=exc.code) from None
