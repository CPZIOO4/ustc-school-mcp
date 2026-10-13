"""Private Rust bridge for business rules, fixed DOM and standalone scheduling."""
from ..desktop import native_core
from ..desktop.model import ModelError
from .session import YoungError


def enabled():
    return native_core.enabled()


def call(operation, **values):
    try:
        return native_core.call(operation, values)
    except ModelError as exc:
        raise YoungError(str(exc), code=exc.code) from None


def policy(action, **values):
    return call('young_policy', action=action, **values)


def parse(action, **values):
    return call('young_parse', action=action, **values)


def io(action, **values):
    from ..mail.config import local_dir
    return call('young_io', action=action, directory=str(local_dir().resolve()), **values)


def runtime_spec():
    """Capture only the runtime selected by the trusted local application."""
    import hashlib
    import os
    from pathlib import Path
    path=Path(os.environ['SCHOOL_NATIVE_CORE'])
    if not path.is_absolute() or not path.is_file():
        raise YoungError('原生核心文件不可用，未安排报名脚本。',code='runtime_failure')
    status=call('status')
    if status.get('protocol')!=1 or not {'young_parse','young_policy','young_job'} <= set(status.get('capabilities',[])):
        raise YoungError('原生核心缺少报名能力，未安排脚本。',code='runtime_failure')
    try:
        with path.open('rb') as stream:
            digest=hashlib.file_digest(stream,'sha256').hexdigest()
    except OSError:
        raise YoungError('原生核心文件无法核验，未安排报名脚本。',code='runtime_failure') from None
    return dict(required=True,path=str(path.resolve()),version=status['version'],protocol=1,sha256=digest)


def restore_runtime(spec):
    """OS Task Scheduler does not inherit the creating application's environment."""
    import hashlib
    import os
    from pathlib import Path
    runtime=spec.get('native_runtime')
    if runtime is None:
        return
    try:
        if not isinstance(runtime,dict) or runtime.get('required') is not True:
            raise ValueError()
        path=Path(runtime['path'])
        if not path.is_absolute() or not path.is_file():
            raise ValueError()
        with path.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=runtime['sha256']:
                raise ValueError()
        os.environ['SCHOOL_NATIVE_CORE']=str(path)
        status=call('status')
        if status.get('version')!=runtime['version'] or status.get('protocol')!=runtime['protocol'] or not {'young_parse','young_policy','young_job'} <= set(status.get('capabilities',[])):
            raise ValueError()
    except (OSError,KeyError,TypeError,ValueError,YoungError):
        raise YoungError('报名计划的原生核心缺失或已变更；脚本停止，请在当前应用重新核对计划。',code='runtime_failure') from None
