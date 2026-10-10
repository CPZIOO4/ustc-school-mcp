"""Explicit local browser onboarding. Secrets never cross the MCP or stdout boundary.

The user logs in; only the recognized client-password form is automated. A durable
write-ahead marker prevents a timeout/crash from creating another credential.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import hashlib
import os
import re
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

from .config import MailConfig, MailError, load_config, local_dir
from .credentials import _dpapi
from .onboarding import connect_and_save, local_status

JOURNAL = 'mail-browser-setup.dpapi'
WEBMAIL = 'https://mail.ustc.edu.cn/'
CREATE_LABELS = ('生成专用密码', 'Generate Specific Password')
RESULT_LABELS = ('专用密码已生成', '专用密码生成成功', '生成专用密码成功', 'Specific password generated')
NEXT = {
    'not_started': '运行 mail-bind --headed --create-client-password，由本人在浏览器登录。',
    'waiting_for_login': '请在本次打开的学校邮箱窗口中登录并完成校方验证；不要重复启动。',
    'ready_to_create': '正在定位客户端专用密码表单；不要手动重复生成。',
    'creation_attempted': '已尝试生成，结果尚未确认。不要再次生成；检查学校专用密码列表，或在本地窗口录入已显示的密码。',
    'captured': '专用密码已加密暂存。运行 mail-bind --resume 重试连接和保存，不生成新密码。',
    'connected': '已保存并通过 IMAP 连接检查；调用 school_mail_check_connection 验证当前连接。',
    'manual_required': '页面未适配、验证未完成或窗口已关闭。按 docs/mail.md 的人工方式继续；不要重复生成。',
    'existing_configuration': '已有邮箱配置；先检查连接。需要更换时明确使用 --replace-existing。',
}


def _load() -> dict:
    path = local_dir() / JOURNAL
    if not path.exists():
        return {'state': 'not_started'}
    try:
        value = json.loads(_dpapi(path.read_bytes(), decrypt=True))
        if not isinstance(value, dict) or value.get('state') not in NEXT:
            raise ValueError
        return value
    except (OSError, ValueError, TypeError, MailError):
        raise MailError('邮箱绑定记录无法读取。请在本机检查，不能通过删除记录重试生成密码。') from None


def _save(data: dict) -> None:
    directory = local_dir()
    directory.mkdir(parents=True, exist_ok=True)
    value = {**data, 'updated_at': datetime.now(timezone.utc).isoformat()}
    protected = _dpapi(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    temporary = directory / (JOURNAL + '.tmp')
    with temporary.open('wb') as handle:
        handle.write(protected)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(directory / JOURNAL)


def status() -> dict:
    try:
        value = _load()
        state = value['state']
        return {'state': state, 'network_checked': False, 'next_step': NEXT[state],
                'credential_captured': state == 'captured', 'updated_at': value.get('updated_at'),
                'note': '历史进度不证明浏览器仍在运行或当前连接有效。'}
    except MailError:
        return {'state': 'record_unreadable', 'network_checked': False,
                'next_step': '在本机检查加密记录；不要删除记录或重新生成密码。'}


@contextmanager
def bind_lock():
    if os.name != 'nt':
        raise MailError('自动邮箱绑定目前仅支持 Windows DPAPI；其他系统使用环境变量接入。')
    import msvcrt
    directory = local_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'mail-browser-setup.lock').open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise MailError('另一个邮箱绑定正在运行；请处理已打开的窗口，不要重复启动。') from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def trusted_mail_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.hostname == 'mail.ustc.edu.cn'
                and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except ValueError:
        return False


def settings_url(url: str) -> str | None:
    if not trusted_mail_url(url):
        return None
    parsed = urlsplit(url)
    if not re.fullmatch(r'/coremail/XT(?:5)?/index\.jsp', parsed.path):
        return None
    # Keep the user's current authenticated URL in memory, never log or export it.
    return urlunsplit(parsed._replace(fragment='setting.security.altpwd'))


def visible_text(scope, labels):
    """Require exactly one visible exact label; never click a fuzzy text match."""
    matches = []
    for label in labels:
        locator = scope.get_by_text(label, exact=True)
        for i in range(locator.count()):
            candidate = locator.nth(i)
            if candidate.is_visible():
                matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def name_form(page):
    """Find the smallest form/dialog with one text field and one Generate action."""
    action = visible_text(page, ('生成', 'Generate'))
    if action is None:
        return None
    container = action
    for _ in range(7):
        container = container.locator('xpath=..')
        if container.evaluate('(e)=>["BODY","HTML"].includes(e.tagName)'):
            return None
        fields = container.locator('input[type=text]:visible, input:not([type]):visible')
        if fields.count() == 1:
            # Avoid mistaking a reauthentication password or OTP form for naming.
            if container.locator('input[type=password]:visible').count():
                return None
            text = container.inner_text()
            if re.search(r'密码名称|Password name|Token name', text, re.I):
                return fields.first, action
    return None


def parse_generated(text: str, expected_address: str = '') -> tuple[str, str] | None:
    """Only parse the generated-password dialog, never arbitrary inbox content."""
    if len(text) > 12000 or not any(label.lower() in text.lower() for label in RESULT_LABELS):
        return None
    if not all(part in text for part in ('IMAP', 'SMTP', '993', '465')):
        return None
    addresses = set(re.findall(r'[A-Za-z0-9_.+\-]+@(?:mail\.)?ustc\.edu\.cn\b', text, re.I))
    if len(addresses) != 1:
        return None
    address = addresses.pop()
    MailConfig(address)
    if expected_address and address.lower() != expected_address.lower():
        raise MailError('网页邮箱与指定地址不同。停止绑定，保留已有配置。')
    # School docs specify 16 alphanumeric characters, displayed in groups of four.
    candidates = set()
    for line in text.splitlines():
        value = re.sub(r'\s+(?:复制|Copy)\s*$', '', line.strip(), flags=re.I)
        if re.fullmatch(r'[A-Za-z0-9]{4}(?:\s+[A-Za-z0-9]{4}){3}|[A-Za-z0-9]{16}', value):
            candidates.add(''.join(value.split()))
    return (address, candidates.pop()) if len(candidates) == 1 else None


def capture_generated(page, expected_address: str = ''):
    if not trusted_mail_url(page.url):
        return None
    title = visible_text(page, RESULT_LABELS)
    if title is None:
        return None
    container = title
    for _ in range(7):
        container = container.locator('xpath=..')
        if container.evaluate('(e)=>["BODY","HTML"].includes(e.tagName)'):
            break
        value = parse_generated(container.inner_text(), expected_address)
        if value:
            return value
    return None


def finish_pending(data: dict) -> dict:
    if data.get('state') != 'captured':
        raise MailError('没有可恢复的已捕获密码。请检查绑定状态；不要重新生成。')
    if data.get('previous_config') != config_fingerprint():
        from .credentials import load_password
        try:
            already_saved = load_config().address == data['address'] and load_password(load_config()) == data['password']
        except MailError:
            already_saved = False
        if not already_saved:
            raise MailError('绑定期间邮箱配置已被其他操作修改。保留暂存密码和现有配置，请在本机核对。')
    # On any failure leave the encrypted pending credential intact for --resume.
    result = connect_and_save(data['address'], data['password'])
    _save({'state': 'connected', 'label': data.get('label')})
    return {'state': 'connected', 'connected': result['connected'], 'saved': True,
            'imap_verified': True, 'smtp_verified': False, 'mail_sent': False,
            'next_step': NEXT['connected']}


def config_fingerprint() -> str:
    digest = hashlib.sha256()
    for name in ('mail.json', 'mail.credentials.dpapi'):
        path = local_dir() / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.exists() else b'absent')
    return digest.hexdigest()


def automate_page(page, *, expected_address: str, timeout: int, label: str, notify) -> dict:
    deadline = time.monotonic() + timeout
    previous_config = config_fingerprint()
    _save({'state': 'waiting_for_login', 'label': label})
    notify(status())
    page.goto(WEBMAIL, wait_until='domcontentloaded', timeout=30000)
    target = None
    while time.monotonic() < deadline:
        target = settings_url(page.url)
        if target:
            break
        page.wait_for_timeout(500)
    if not target:
        _save({'state': 'manual_required', 'label': label})
        return status()
    page.goto(target, wait_until='domcontentloaded', timeout=30000)
    _save({'state': 'ready_to_create', 'label': label})
    notify(status())
    # UI recognition is bounded. Never keep trying a mutation because it timed out.
    button = None
    for _ in range(20):
        if not trusted_mail_url(page.url):
            break
        button = visible_text(page, CREATE_LABELS)
        if button:
            break
        page.wait_for_timeout(500)
    if button is None:
        _save({'state': 'manual_required', 'label': label})
        return status()
    button.click(timeout=5000)  # Opens naming form; does not authorize deletion/reset.
    form = None
    for _ in range(10):
        if not trusted_mail_url(page.url):
            break
        form = name_form(page)
        if form:
            break
        page.wait_for_timeout(500)
    if form is None:
        _save({'state': 'manual_required', 'label': label})
        return status()
    field, generate = form
    field.fill(label)
    if not trusted_mail_url(page.url):
        raise MailError('邮箱页面来源改变，已停止。')
    _save({'state': 'creation_attempted', 'label': label})
    generate.click(timeout=5000)  # Exactly one final creation attempt across restarts.
    for _ in range(30):
        value = capture_generated(page, expected_address)
        if value:
            address, password = value
            pending = {'state': 'captured', 'address': address, 'password': password, 'label': label,
                       'previous_config': previous_config}
            _save(pending)  # Persist once-only secret before any fallible IMAP operation.
            notify(status())
            return finish_pending(pending)
        page.wait_for_timeout(500)
    return status()  # Ambiguous creation: never repeat and never delete other tokens.


def run(*, headed: bool = False, create_client_password: bool = False, resume: bool = False,
        address: str = '', replace_existing: bool = False, timeout: int = 600, notify=None) -> dict:
    notify = notify or (lambda _: None)
    if os.environ.get('SCHOOL_MAIL_PASSWORD'):
        raise MailError('当前进程设置了 SCHOOL_MAIL_PASSWORD，会覆盖已保存凭据。请先在本机清除该环境变量，再进行浏览器绑定。')
    if not 30 <= timeout <= 900:
        raise MailError('登录等待时间必须在 30 到 900 秒之间。')
    if address:
        MailConfig(address)
    with bind_lock():
        data = _load()
        if data['state'] == 'captured':
            if address and address.lower() != data.get('address', '').lower():
                raise MailError('暂存密码与指定邮箱不匹配；停止处理。')
            return finish_pending(data)
        if resume:
            raise MailError('没有待恢复的密码；先查看 mail-bind-status。')
        if data['state'] == 'creation_attempted':
            return status()
        if local_status()['configured'] and not replace_existing:
            return {'state': 'existing_configuration', 'next_step': NEXT['existing_configuration'], 'network_checked': False}
        if not (headed and create_client_password):
            raise MailError('首次浏览器绑定需要 --headed --create-client-password：本人登录后创建一个本项目专用密码。')
        if not address and replace_existing:
            try:
                address = load_config().address
            except MailError:
                pass
        from playwright.sync_api import sync_playwright
        from school_mcp.browser import chrome_browser
        label = 'SchoolMCP-' + datetime.now(timezone.utc).strftime('%Y%m%d') + '-' + uuid.uuid4().hex[:6]
        try:
            with sync_playwright() as p, chrome_browser(p, headless=False) as browser:
                context = browser.new_context(locale='zh-CN', accept_downloads=False, service_workers='block')
                # Separate temporary context. No personal Chrome profile or plaintext cookies.
                page = context.new_page()
                try:
                    result = automate_page(page, expected_address=address, timeout=timeout, label=label, notify=notify)
                except Exception:
                    # Handle inside the browser lifetime so a once-only secret can
                    # still be copied by its owner if capture or disk storage failed.
                    current = _load()
                    if current['state'] not in ('creation_attempted', 'captured', 'connected'):
                        _save({'state': 'manual_required', 'label': label})
                    result = status()
                    result['operation_failed'] = True
                if result['state'] == 'creation_attempted' and not page.is_closed():
                    result.update(requires_user_action=True, manual_window_seconds=180,
                        next_step=NEXT['creation_attempted'] + ' 窗口保留最多三分钟；若密码仍可见，请用本机 setup_mail.py 保存，然后关闭浏览器。')
                    notify(result)
                    end = time.monotonic() + 180
                    while time.monotonic() < end and not page.is_closed():
                        try:
                            page.wait_for_timeout(500)
                        except Exception:
                            break
                return result
        except Exception:
            # Browser/library exceptions can contain session URLs or form values.
            current = _load()
            if current['state'] not in ('creation_attempted', 'captured', 'connected'):
                _save({'state': 'manual_required', 'label': label})
            result = status()
            result['operation_failed'] = True
            return result
