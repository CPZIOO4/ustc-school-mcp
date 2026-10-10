"""Minimal first-use routing; local presence, runtime and live checks are separate."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile

from .diagnostics import SERVICES, diagnose
from .mail.config import local_dir
from .mail.onboarding import local_status
from .runtime_version import MONITOR

SESSIONS = {s: f'{s}.session.dpapi' for s in ('bb', 'jw', 'library')} | {
    s: f'{s}-session.dpapi' for s in ('nan7', 'icourse', 'young', 'finance')}


def command(*args: str) -> list[str]:
    return [sys.executable, '-m', 'school_mcp', *args]


def local_report(services: list[str]) -> dict:
    directory = local_dir()
    identity = (directory / 'ustc-identity.credentials.dpapi').is_file()
    checks = []
    for service in services:
        item = {'service': service, 'network_checked': False, 'connection_state': 'unchecked'}
        if service in ('teach', 'icourse'):
            item.update(state='public_read_ready', next_tool=f'school_{service}_check_connection')
        elif service == 'mail':
            status = local_status()
            item.update(state=status['state'], next_tool='school_mail_check_connection' if status['configured'] else 'school_mail_setup_guide')
            if not status['configured']:
                item.update(requires_user_action=True, next_command=command('mail-bind', '--headed', '--create-client-password'),
                            action='在单独打开的学校邮箱窗口中登录并完成校方验证。')
        elif (directory / SESSIONS[service]).is_file():
            item.update(state='session_unchecked', next_tool=f'school_{service}_check_connection')
        elif identity:
            item.update(state='login_required', next_tool=f'school_{service}_reconnect',
                        action='用户已授权登录该站点时调用；完成后检查连接。')
        else:
            item.update(state='identity_required', requires_user_action=True,
                        next_command=command('bb-setup'), action='在用户本机交互终端输入统一身份凭据；不要通过 AI 工具 stdin 传入。')
        checks.append(item)
    return {'checks': checks, 'network_checked': False, 'full_login_platform_supported': os.name == 'nt',
            'required_environment': {'SCHOOL_MCP_LOCAL_DIR': str(directory), 'PYTHONUTF8': '1'},
            'mail_verification_auto_enabled': False,
            'note': '本地存在性不是连接成功。邮箱客户端密码不等于统一身份密码；启用邮件验证码回退需用户授权。'}


async def protocol_check(services: list[str], expected_fingerprint: str) -> list[dict]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    result = []
    # Never use the user's mailbox, sessions, jobs or inherited mail secret in this check.
    with tempfile.TemporaryDirectory(prefix='school-mcp-first-use-') as tmp:
        environment = {**os.environ, 'SCHOOL_MCP_LOCAL_DIR': tmp, 'SCHOOL_MAIL_PASSWORD': '',
                       'PYTHONUTF8': '1', 'SCHOOL_MCP_BROWSER_HEADED': '0'}
        with open(os.devnull, 'w', encoding='utf-8') as errors:
            for service in services:
                async def check():
                    params = StdioServerParameters(command=sys.executable,
                        args=['-m', 'school_mcp', 'serve' if service == 'mail' else service + '-serve'], env=environment)
                    async with stdio_client(params, errlog=errors) as (reader, writer):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            names = {t.name for t in tools.tools}
                            runtime_tool = f'school_{service}_runtime_status'
                            expected = {f'school_{service}_status', f'school_{service}_check_connection', runtime_tool}
                            if not expected <= names:
                                raise RuntimeError('Missing required tool')
                            status = await session.call_tool(f'school_{service}_status', {})
                            if status.isError:
                                raise RuntimeError('Status tool failed')
                            runtime = await session.call_tool(runtime_tool, {'expected_fingerprint': expected_fingerprint})
                            evidence = runtime.structuredContent or {}
                            ready = not runtime.isError and evidence.get('state') == 'current' and evidence.get('expected_matches') is True
                            return {'service': service, 'ready': ready, 'tool_count': len(names),
                                    'state': 'ready' if ready else 'runtime_version_mismatch',
                                    'runtime': evidence}
                try:
                    result.append(await asyncio.wait_for(check(), timeout=12))
                except Exception:
                    result.append({'service': service, 'ready': False, 'state': 'stdio_failed',
                                   'action': '运行 bootstrap.py install 修复依赖后重试；不启动账号登录。'})
    return result


def runtime_check(services: list[str]) -> dict:
    expected = MONITOR.report()['disk_fingerprint']
    checks = asyncio.run(protocol_check(services, expected)) if expected else []
    chrome = {'ready': False, 'headless': True, 'network_checked': False}
    try:
        from playwright.sync_api import sync_playwright
        from .browser import chrome_browser
        with sync_playwright() as p, chrome_browser(p, headless=True) as browser:
            page = browser.new_page()
            page.goto('about:blank')
            chrome['ready'] = True
    except Exception:
        chrome.update(state='chrome_unavailable', action='安装本机 Google Chrome 后重试；无需浏览器控制扩展。')
    evidence = MONITOR.report(expected)
    return {'ready': bool(checks) and all(c['ready'] for c in checks) and chrome['ready']
                    and evidence['state'] == 'current', 'stdio': checks, 'chrome': chrome, 'version_check': evidence,
            'client_verification': {'required': True, 'expected_fingerprint': expected,
                                    'tools': [f'school_{s}_runtime_status' for s in services],
                                    'success_conditions': ['state=current', 'expected_matches=true'],
                                    'action': '按当前 AI 客户端支持的方式重载这些 MCP 后，在客户端逐一调用上述工具并传入 expected_fingerprint；工具不存在表示仍是旧版。'},
            'isolated_private_directory': True, 'network_checked': False,
            'note': '这是协议和环境测试，不证明当前 AI 客户端已加载配置或学校账号已连接。'}


def first_use(services: list[str] | None = None, *, verify_runtime: bool = False,
              check_connections: bool = False) -> dict:
    selected = list(dict.fromkeys(services or ('mail', 'teach')))
    if any(s not in SERVICES for s in selected):
        raise ValueError('Unsupported service')
    report = local_report(selected)
    if verify_runtime:
        report['runtime'] = runtime_check(selected)
    if check_connections:
        report['connections'] = diagnose(selected)
        report['network_checked'] = True
    report['next_step'] = '按 checks 的 state/next_tool/next_command 处理；只检查用户要使用的服务。'
    return report
