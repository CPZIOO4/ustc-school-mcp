"""Fixed actionable authentication reasons, shared by UI and MCP."""
MESSAGES = {'login_password_required': '学校已返回密码登录页。点击“账号与连接 → 对应平台 → 打开验证窗口”，在官方页面输入统一身份账号和密码；出现设备信任选项时勾选。验证成功后应用自动保存会话。', 'login_email_required': '学校正在要求邮件验证码。已关联并授权的邮箱验证会先自动尝试；若仍停在此处，点击“打开验证窗口”，选择邮箱验证并填写最新验证码。', 'login_captcha_required': '学校正在要求图形或滑块验证。点击“打开验证窗口”，在官方页面完成图形或滑块；通过后继续登录，应用会自动检查连接。', 'login_phone_required': '学校正在要求手机扫码或动态口令。点击“打开验证窗口”，按官方页面提示扫码或填写手机动态口令。', 'login_page_unrecognized': '暂未识别到登录成功或明确的验证控件。点击“查看登录页面”检查是否停在跳转、网络错误或授权页；不要反复输入密码。'}

def reason(detail):
    # Only select a fixed label; never echo supplied prose into the result.
    if '图形' in detail or '滑块' in detail:return 'login_captcha_required'
    if '动态口令' in detail or '扫码' in detail:return 'login_phone_required'
    if '邮件验证码' in detail:return 'login_email_required'
    if '重新登录' in detail or '密码输入' in detail:return 'login_password_required'
    return 'manual_verification_required'
