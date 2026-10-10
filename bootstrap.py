"""Dependency-free entry point for an AI deploying a downloaded checkout."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SERVICES = ('mail', 'bb', 'jw', 'library', 'teach', 'nan7', 'icourse', 'young', 'finance')
NAMES = {s: 'ustc-' + s for s in SERVICES} | {'nan7': 'nan7market', 'icourse': 'icourse'}


def python_path() -> Path:
    return ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def uv_command() -> list[str] | None:
    if executable := shutil.which('uv'):
        return [executable]
    import importlib.util
    return [sys.executable, '-m', 'uv'] if importlib.util.find_spec('uv') else None


def configuration(services: list[str], private_dir: Path) -> dict:
    executable = python_path()
    if not executable.is_file():
        raise ValueError('先运行 python bootstrap.py install。')
    if any(service not in SERVICES for service in services):
        raise ValueError('未知服务。')
    return {'mcpServers': {NAMES[s]: {
        'command': str(executable), 'args': ['-m', 'school_mcp', 'serve' if s == 'mail' else s + '-serve'],
        'env': {'SCHOOL_MCP_LOCAL_DIR': str(private_dir.resolve()), 'PYTHONUTF8': '1',
                'SCHOOL_MCP_BROWSER_HEADED': '0'},
    } for s in dict.fromkeys(services)}}


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'install', 'config', 'verify'))
    parser.add_argument('--service', action='append', choices=SERVICES)
    parser.add_argument('--private-dir', type=Path, default=ROOT / '.local')
    args = parser.parse_args()
    selected = list(dict.fromkeys(args.service or SERVICES))
    private = args.private_dir.expanduser().resolve()
    environment = {**os.environ, 'SCHOOL_MCP_LOCAL_DIR': str(private), 'PYTHONUTF8': '1',
                   'SCHOOL_MCP_BROWSER_HEADED': '0'}
    if args.command == 'check':
        ready = python_path().is_file()
        print(json.dumps({'python_supported': sys.version_info >= (3, 11), 'uv_available': bool(uv_command()),
            'environment_exists': ready, 'full_login_platform_supported': os.name == 'nt',
            'network_checked': False, 'next_command': [sys.executable, str(ROOT / 'bootstrap.py'),
                'verify' if ready else 'install'],
            'note': '完整账号接入目前支持 Windows。尚未检查依赖、Chrome 或学校连接；verify 验证依赖与 MCP。'}, ensure_ascii=False))
        return 0 if sys.version_info >= (3, 11) else 1
    if args.command == 'install':
        uv = uv_command()
        if uv is None:
            print(json.dumps({'state': 'uv_required', 'next_command': [sys.executable, '-m', 'pip', 'install', '--user', 'uv'],
                'note': '安装 uv 后重新运行 install；不会自动修改系统 Python 或客户端配置。'}, ensure_ascii=False))
            return 1
        # The lockfile controls dependency resolution, including a ZIP download without .git.
        result = subprocess.run([*uv, 'sync', '--locked', '--cache-dir', str(private / 'uv-cache')],
                                cwd=ROOT, env=environment)
        return result.returncode
    if args.command == 'config':
        try:
            print(json.dumps(configuration(selected, private), ensure_ascii=False, indent=2))
            return 0
        except ValueError as exc:
            print(json.dumps({'state': 'install_required', 'note': str(exc)}, ensure_ascii=False))
            return 1
    if not python_path().is_file():
        print(json.dumps({'state': 'install_required', 'next_command': [sys.executable, str(ROOT / 'bootstrap.py'), 'install']}))
        return 1
    try:
        return subprocess.run([str(python_path()), '-m', 'school_mcp', 'first-use', '--verify-runtime',
                               *[x for service in selected for x in ('--service', service)]],
                              cwd=ROOT, env=environment, timeout=180).returncode
    except (OSError, subprocess.TimeoutExpired):
        print(json.dumps({'state': 'runtime_unavailable', 'note': '环境无法启动或验证超时；检查安装结果，不要启动登录。'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
