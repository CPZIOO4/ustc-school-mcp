"""Read a requested USTC login code without returning or persisting the code."""

from __future__ import annotations

import re
import time
from email.utils import getaddresses
from urllib.parse import urlsplit

from ..mail.client import MailClient
from ..mail.config import MailError, load_config
from .identity import IDENTITY_HOSTS

EMAIL_OPTION = re.compile(r"^(?:可信邮箱|信任邮箱|安全邮箱)(?:验证|认证)?$|^(?:邮箱验证|邮箱认证|邮箱验证码|邮件验证|Trusted email|Email verification|Email authentication)$", re.I)
EMAIL_MODE = re.compile(r"可信邮箱|信任邮箱|安全邮箱|邮箱验证|邮箱认证|邮箱验证码|邮件验证|Trusted email|Email verification|Email authentication", re.I)
SEND_CODE = re.compile(r"^(获取验证码|发送验证码|获取邮箱验证码|发送邮箱验证码|Send(?: verification)? code|Get(?: verification)? code)$", re.I)
SUBMIT_CODE = re.compile(r"^(验证|确认|确定|立即验证|立即登录|登录|Verify|Confirm|Sign In|Log In)$", re.I)
ADDRESS = re.compile(r"[A-Za-z0-9_.+*•…-]+@[A-Za-z0-9.*•…-]+\.[A-Za-z*•…]{2,}")
CODE_CONTEXT = r"验证码|校验码|动态口令|动态密码|verification\s+code|one[- ]time\s+(?:password|code)"
CODE = re.compile(rf"(?:{CODE_CONTEXT})[\s:：,，为是\[\]【】()（）-]{{0,30}}([0-9]{{4,8}})(?![0-9])", re.I)


def matches_address(address: str, displayed: str) -> bool:
    name, separator, domain = displayed.strip().partition("@")
    # A wholly masked account or domain does not identify the authorized mailbox.
    if not separator or domain.lower() != address.partition("@")[2].lower() or not re.search(r"[A-Za-z0-9]", name):
        return False
    pattern = re.escape(displayed.strip()).replace(r"\*", "[^@]*").replace("•", "[^@]*").replace("…", "[^@]*")
    return re.fullmatch(pattern, address, re.I) is not None


def confirms_target(address: str, targets: list[str]) -> bool:
    # Multiple different destinations are ambiguous even if one happens to match.
    return bool(targets) and all(matches_address(address, target) for target in targets)


def eligible_header(message: dict, address: str) -> bool:
    senders = getaddresses([message.get("from", "")])
    recipients = getaddresses([message.get("to", ""), message.get("cc", "")])
    return (
        len(senders) == 1 and senders[0][1].lower().partition("@")[2] in {"ustc.edu.cn", "mail.ustc.edu.cn"}
        and address.lower() in {email.lower() for _, email in recipients}
        and re.search(r"验证|动态口令|动态密码|verification|one[- ]time|login code", message.get("subject", ""), re.I) is not None
    )


def extract_code(message: dict, address: str) -> str | None:
    if message.get("body_truncated") or not eligible_header(message, address):
        return None
    subject = message.get("subject", "")
    body = message.get("body", "")
    context = subject + "\n" + body
    if re.search(r"找回密码|重置密码|绑定邮箱|更换邮箱|password\s+reset|account\s+recovery", context, re.I):
        return None
    if not re.search(r"统一身份|统一认证|身份认证|SourceID|USTC.*(?:login|auth)|(?:login|auth).*USTC", context, re.I):
        return None
    codes = set(CODE.findall(context))
    return next(iter(codes)) if len(codes) == 1 else None


class MailCodeRequest:
    """The UID watermark is captured BEFORE the page requests an email."""

    def __init__(self, client: MailClient):
        self.client = client
        snapshot = client.verification_snapshot()
        self.validity = snapshot["uid_validity"]
        self.watermark = snapshot["next_uid"] - 1
        self.examined: set[int] = set()

    def poll(self) -> str | None:
        result = self.client.verification_headers(self.watermark + 1, self.validity)
        if result["uid_validity"] != self.validity:
            raise MailError("邮箱编号在验证码等待期间改变，请在学校窗口完成验证。")
        candidates = []
        for message in result["messages"]:
            uid = message["uid"]
            if uid <= self.watermark or uid in self.examined:
                continue
            self.examined.add(uid)
            if not eligible_header(message, self.client.config.address):
                continue
            full = self.client.read(uid, self.validity, max_chars=10000)
            code = extract_code(full, self.client.config.address)
            if not code:
                raise MailError("新的认证邮件无法唯一确定本次验证码，请人工完成验证。")
            candidates.append(code)
        # Concurrent login-code emails are ambiguous: never guess which request they belong to.
        if len(candidates) > 1:
            raise MailError("出现多封新的认证验证码邮件，请在学校窗口确认本次验证码。")
        return candidates[0] if candidates else None


def unique_visible(locator):
    visible = [item for item in locator.all() if item.is_visible()]
    return visible[0] if len(visible) == 1 else None


def control(page, pattern):
    return unique_visible(page.get_by_role("button", name=pattern)) or unique_visible(page.get_by_text(pattern, exact=True))


class EmailVerification:
    def __init__(self, enabled: bool, notify, before_submit, expected_address: str | None = None):
        self.enabled = enabled
        self.notify = notify
        self.before_submit = before_submit
        self.selected = False
        self.requested = False
        self.submitted = False
        self.blocked = False
        self.request: MailCodeRequest | None = None
        self.pending_code: str | None = None
        self.deadline = 0.0
        self.next_poll = 0.0
        self.last_stage = None
        self.address: str | None = None
        self.expected_address = expected_address

    def report(self, stage, detail):
        if self.last_stage != stage:
            self.notify(stage, detail)
            self.last_stage = stage

    def stop(self, detail):
        self.blocked = True
        self.pending_code = None
        self.report("waiting_for_verification", detail)

    def observe_result(self, text: str) -> None:
        if self.submitted and not self.blocked and re.search(r"验证码.*(?:错误|不正确|无效|过期)|(?:错误|无效|过期).*验证码|invalid.*code|code.*expired", text, re.I):
            self.stop("学校未接受本次邮件验证码，已停止自动重试，请在窗口完成验证。")

    def step(self, page) -> None:
        if not self.enabled or self.blocked or self.submitted:
            return
        try:
            parsed = urlsplit(page.url)
            valid_origin = parsed.scheme == "https" and parsed.hostname in IDENTITY_HOSTS and parsed.port in (None, 443) and not parsed.username and not parsed.password
        except ValueError:
            valid_origin = False
        if not valid_origin:
            if self.requested:
                self.stop("认证页面已离开已允许的学校来源，请人工完成验证。")
            return
        # This fallback only handles a challenge after password login, never a recovery/setup form.
        dialog = unique_visible(page.get_by_role("dialog"))
        surface = dialog or page
        if surface.locator('input[type="password"]:visible').count():
            if self.requested:
                self.stop("认证页面已回到密码输入，请人工完成验证。")
            return
        text = dialog.inner_text(timeout=3000) if dialog else page.locator("body").inner_text(timeout=3000)
        if re.search(r"重置密码|找回密码|绑定邮箱|更换邮箱|Reset password|Bind email", text, re.I):
            if self.requested:
                self.stop("页面已切换为账号设置，请人工完成验证。")
            return
        if surface.locator('input[placeholder*="图形"]:visible, input[name="captcha"]:visible').count() or re.search(r"拖动.*滑块|slide.*verify", text, re.I):
            self.report("waiting_for_verification", "学校要求图形或滑块验证，请在窗口完成；邮件验证码流程随后继续。")
            return
        if not EMAIL_MODE.search(text):
            if self.requested:
                self.stop("本次邮箱验证页面已改变，请人工完成验证。")
            return
        try:
            config = load_config()
            if not self.expected_address or config.address != self.expected_address:
                self.stop("邮件验证未绑定当前邮箱；请在本机明确启用后重试。")
                return
            addresses = ADDRESS.findall(text)
            email_field = unique_visible(surface.locator('input[type="email"]:visible, input[name="email"]:visible, input[name="mbemail"]:visible'))
            if email_field and email_field.input_value().strip():
                addresses = [email_field.input_value().strip()]
            if self.requested and (config.address != self.address or not confirms_target(self.address, addresses)):
                self.stop("本次验证的页面邮箱或本机配置已改变，请人工完成验证。")
                return
            if not self.requested:
                send = control(surface, SEND_CODE)
                if send is None or (not addresses and email_field is None):
                    if not self.selected:
                        option = control(surface, EMAIL_OPTION)
                        if option:
                            option.click()
                            self.selected = True
                            self.report("selecting_email_verification", "已选择学校提供的邮箱验证方式。")
                    return
                if email_field:
                    current = email_field.input_value().strip()
                    if current and not matches_address(config.address, current):
                        self.stop("学校验证邮箱与已接入邮箱不一致，请在窗口完成验证。")
                        return
                    if not current:
                        email_field.fill(config.address)
                    addresses = [email_field.input_value().strip()]
                if not confirms_target(config.address, addresses):
                    self.stop("无法确认学校的验证邮箱对应已接入邮箱，请在窗口完成验证。")
                    return
                if not send.is_enabled():
                    return
                self.request = MailCodeRequest(MailClient(config))
                self.address = config.address
                # Set before clicking: a navigation error must not cause a second email request.
                self.requested = True
                self.deadline = time.monotonic() + 180
                send.click()
                self.report("waiting_for_email_code", "已请求学校发送邮件验证码，正在只读检查本次新到达的认证邮件。")
                return
            if time.monotonic() >= self.deadline:
                self.stop("等待学校认证邮件超时；不会重复发送，请在窗口完成验证。")
                return
            if self.pending_code is None and time.monotonic() >= self.next_poll:
                self.next_poll = time.monotonic() + 4
                self.pending_code = self.request.poll()
            if self.pending_code is None:
                return
            button = control(surface, SUBMIT_CODE)
            inputs = [item for item in surface.locator('input[autocomplete="one-time-code"]:visible, input[name="emailCode"]:visible, input[name="code"]:visible, input[placeholder*="验证码"]:visible, app-verification input:visible').all() if item.is_visible()]
            single = len(inputs) == 1
            separate = len(inputs) == len(self.pending_code) and all(item.get_attribute("maxlength") == "1" for item in inputs)
            if button is None or not button.is_enabled() or not (single or separate):
                return
            self.before_submit(page)
            if single:
                inputs[0].fill(self.pending_code)
            else:
                for item, digit in zip(inputs, self.pending_code):
                    item.fill(digit)
            self.pending_code = None
            self.submitted = True
            button.click()
            self.report("email_code_submitted", "已将本次邮件验证码提交到学校认证页面，正在等待登录结果。")
        except MailError:
            self.stop("无法安全读取本次认证邮件，请在学校窗口完成验证；已有信任设备状态仍保留。")
