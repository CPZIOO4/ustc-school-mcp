"""Offline startup-source evidence for long-lived MCP processes, including ZIP installs."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
from typing import Any
import uuid


def digest(values: dict) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def snapshot(package: Path) -> dict:
    """Read only package Python sources and known deployment manifests, never private state."""
    try:
        files = {}
        total = 0

        def read(path):
            nonlocal total
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('unverifiable_source')
            with path.open('rb') as handle:
                data = handle.read(2 * 1024 * 1024 + 1)
            total += len(data)
            if len(data) > 2 * 1024 * 1024 or total > 16 * 1024 * 1024:
                raise ValueError('source_limit')
            return hashlib.sha256(data).hexdigest()

        if package.is_symlink() or not package.is_dir():
            raise ValueError('unverifiable_source')
        def walk_error(error):
            raise error
        for directory, dirs, names in os.walk(package, followlinks=False, onerror=walk_error):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
            if any((Path(directory) / d).is_symlink() for d in dirs):
                raise ValueError('unverifiable_source')
            for name in sorted(names):
                if name.startswith('.') or not name.endswith('.py'):
                    continue
                path = Path(directory) / name
                files[path.relative_to(package).as_posix()] = read(path)
                if len(files) > 2000:
                    raise ValueError('source_limit')
        if '__init__.py' not in files or 'runtime_version.py' not in files:
            raise ValueError('incomplete_source')
        # A wheel has no project manifests; do not look around site-packages.
        manifests = {}
        if package.parent.name == 'src':
            root = package.parent.parent
            for name in ('pyproject.toml', 'uv.lock'):
                path = root / name
                manifests[name] = read(path) if path.exists() or path.is_symlink() else None
        versions = {}
        for name in ('school-mcp', 'mcp', 'httpx', 'beautifulsoup4', 'playwright'):
            try:
                versions[name] = metadata.version(name)
            except metadata.PackageNotFoundError:
                versions[name] = None
        source = digest(files)
        dependencies = digest({'manifests': manifests, 'installed_versions': versions})
        return {'source_fingerprint': source, 'dependency_fingerprint': dependencies,
                'fingerprint': digest({'source': source, 'dependencies': dependencies}),
                'package_version': versions['school-mcp'], 'manifests': manifests,
                'source_file_count': len(files)}
    except (OSError, ValueError, RuntimeError):
        return {'fingerprint': None, 'source_fingerprint': None, 'dependency_fingerprint': None}


class RuntimeMonitor:
    def __init__(self, package: Path | None = None):
        self.package = package or Path(__file__).parent
        self.instance_id = uuid.uuid4().hex
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.startup = snapshot(self.package)

    def report(self, expected_fingerprint: str | None = None) -> dict:
        if expected_fingerprint is not None and (not isinstance(expected_fingerprint, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_fingerprint)):
            raise ValueError('expected_fingerprint must be a 64-character lowercase SHA-256')
        current = snapshot(self.package)
        verified = bool(self.startup['fingerprint'] and current['fingerprint'])
        changed = self.startup['fingerprint'] != current['fingerprint'] if verified else None
        expected_matches = (self.startup['fingerprint'] == expected_fingerprint
                            if verified and expected_fingerprint else None)
        manifests_changed = self.startup.get('manifests') != current.get('manifests') if verified else None
        if not verified:
            state, actions = 'check_failed', ['check_source_files', 'verify_local', 'verify_client']
        elif changed:
            state = 'restart_required'
            actions = (['sync_dependencies'] if manifests_changed else []) + ['verify_local', 'reload_client', 'verify_client']
        elif expected_matches is False:
            state, actions = 'expected_mismatch', ['check_client_configuration', 'reload_client', 'verify_client']
        else:
            state, actions = 'current', []
        return {'state': state, 'restart_required': changed, 'instance_id': self.instance_id,
                'started_at': self.started_at, 'package_version': self.startup.get('package_version'),
                'startup_fingerprint': self.startup['fingerprint'], 'disk_fingerprint': current['fingerprint'],
                'startup_source_fingerprint': self.startup['source_fingerprint'],
                'disk_source_fingerprint': current['source_fingerprint'],
                'dependencies_changed': (self.startup['dependency_fingerprint'] != current['dependency_fingerprint']) if verified else None,
                'dependency_sync_required': manifests_changed, 'expected_matches': expected_matches,
                'actions': actions, 'network_checked': False, 'credentials_read': False,
                'scope': 'calling_process_startup_vs_disk',
                'note': '指纹记录进程启动时的项目源码及依赖元数据，不是内存代码转储或远端最新版本检查。current 不证明学校连接有效；独立新进程不能证明当前客户端已重载。'}


# Imported by package __init__ before service modules, not on the first status call.
MONITOR = RuntimeMonitor()


def install(mcp, service: str) -> None:
    from mcp.types import ToolAnnotations

    def runtime_status(expected_fingerprint: str | None = None) -> dict[str, Any]:
        return {'service': service, **MONITOR.report(expected_fingerprint)}

    mcp.add_tool(runtime_status, name=f'school_{service}_runtime_status',
                 description='离线检查本服务进程启动时的源码/依赖指纹与当前磁盘。restart_required 表示需要重载；'
                             '更新后使用 bootstrap verify 给出的 expected_fingerprint 在当前客户端调用本工具，核对 current、expected_matches=true。'
                             'instance_id 可区分进程；工具不存在说明需先重载旧版。不会重启、联网、读取凭据或证明账号连接。',
                 annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
