---
name: school-onboarding
description: 在用户已使用支持本地命令和 stdio MCP 的 AI 客户端时，部署此学校 MCP 项目、生成客户端配置、引导本机邮箱绑定并验证首次连接。适用于首次安装或配置缺失；已有连接的日常学校业务使用 school-services。
---

# 从下载到首次连接

先读项目根目录的 [AGENTS.md](../../../AGENTS.md) 和 [AI 部署步骤](../../../docs/ai-setup.md)。用户提供项目文件夹并要求部署时，直接完成可执行的本机安装、配置生成和隔离验证；到个人网页登录或本机凭据录入时再交接。

## 固定顺序

1. `python bootstrap.py check` → 缺基础环境则按返回修复 → `install` → `verify`。非 Windows 明确完整登录不支持；不要用未加密文件模拟 DPAPI。ZIP 不需要 Git。
2. `python bootstrap.py config --service mail --service teach`。按当前客户端实际格式合并配置，保留无关配置；全部阶段保持同一私人目录。其他站点按用户需求追加。重新加载后实际调用工具，不能拿 CLI 成功冒充客户端成功。
3. `school_mail_setup_guide`。已有邮箱先 `school_mail_check_connection`；有效则跳过生成。用户选择绑定并创建专用密码时，执行返回的 `browser_setup_argv`，沿用 `required_environment`，让本人在独立 Chrome 窗口登录。不要读取聊天历史中的凭据、截图秘密弹窗或将密码作为工具参数。
4. 查看 `school_mail_bind_status`，按下表继续。无需模型操作网页控件；脚本负责生成、加密捕获和连接验证。它不发邮件，也不自动启用邮件验证码回退。
5. 成功后实际调用 `school_mail_check_connection`、`school_teach_check_connection`。返回部署结果、真实连接结果和剩余人工步骤，区分模拟验证与真实账号验收。

| 绑定状态 | 下一步 |
| --- | --- |
| `waiting_for_login` | 用户在本次窗口登录、处理校方验证；不启动第二个脚本。历史状态不证明进程仍在运行，核对原命令是否退出。 |
| `ready_to_create` | 等脚本完成；不让模型并行点生成。 |
| `captured` | 密码已加密暂存，运行 `mail-bind --resume`；检查返回的失败原因，遵守网络冷却。 |
| `creation_attempted` | 已请求生成但结果不明；停止，不重复调用生成、不删除日志绕过保护。用户在本机检查密码列表，按邮箱文档处理。 |
| `manual_required` / `record_unreadable` | 读 [邮箱接入说明](../../../docs/mail.md) 的人工处理步骤。不得改控件匹配强行继续。 |
| `existing_configuration` | 先检查已有连接；更换凭据需要用户意图明确，再使用 `--replace-existing`。 |
| `connected` | 加密保存及 IMAP 检查完成，随后用当前客户端工具验证；不等于已测试 SMTP 发送。 |

## 按需接入学校账号

`first-use --service jw` 等命令给出下一步。`identity_required` 时交给用户可见终端运行 `bb-setup`，用户自己输入；不要通过 AI 工具 stdin 收集密码。已有凭据且获登录授权，用该站点 `reconnect`，依返回状态等待；人工验证时使用对应 `*-login --headed`。公共教务通知和评课读取无需账号。

只有用户授权邮件验证码回退且邮箱真实连接成功时，执行 `bb-email-verification --enable`。具体边界见邮箱文档。后续查询和办理转到 [school-services](../school-services/SKILL.md)，安装授权不等于发信、报名或提交作业授权。
