# 邮件发送与回复

本模块提供固定接口：准备、校验、单次发送、查询本地回执、查找回复。操作步骤见 [邮件 Skill](../.agents/skills/school-services/references/mail-workflows.md)。无需浏览器，复用 [邮箱接入](mail.md) 保存的客户端专用密码，SMTP 固定为 `mail.ustc.edu.cn:465`，验证 TLS 证书。

| 工具 | 输入重点 | 结果及效果 |
| --- | --- | --- |
| `school_mail_prepare` | to、subject、body，可选 cc/bcc/attachments | `needs_input` 或 `ready`、固定编号、内容摘要和预览；本机加密写入 |
| `school_mail_prepare_reply` | 原信 mailbox/uid/uid_validity、正文、可选附件 | 只读原信，按 Reply-To/From 准备单人回复和线程头；不发送 |
| `school_mail_send` | draft_id、content_sha256 | 实际 SMTP 发送，必须有用户授权；每草稿最多一次尝试 |
| `school_mail_send_status` | draft_id，可选 include_preview | 本机状态，不联网，默认不返回正文及附件 |
| `school_mail_find_replies` | draft_id，可选 mailbox/limit | 只读线程回复摘要，默认 INBOX、10 封，最多 50；不自动等待 |
| `school_mail_check_sent_copy` | draft_id，可选 mailbox | 只读检查服务器副本；默认识别唯一 Sent 标志，返回匹配副本的 UID 三元组 |
| `school_mail_save_sent_copy` | draft_id，可选 mailbox | 在用户归档授权下保存缺失副本；只对 accepted/partial，先检查、至多一次 APPEND |

原有 search/read/download 继续收信、读正文和保存指定附件，保留未读状态。查回复先 IMAP HEADER 搜索，再校验完整 Message-ID token；只匹配线程，不认证发件人；缺少线程头的回复需另行窄搜索。

收件人须为纯邮箱地址数组，目前支持常见 ASCII 地址，不支持国际化邮箱本地部分、显示名或地址组。每封至多 20 个收件人、10 个附件，正文 100000 字符，完整 MIME 上限 20 MiB；这是本项目限制，不是学校承诺的额度。附件准备时读取并固定，原文件改变不影响发送。Bcc 仅存在 envelope，不写入 MIME。正文预览上限 6000 字符并标记截断。

## 状态和存储

`ready → sending → accepted / partial / rejected / failed_before_data / unknown`。

`accepted` 表示 DATA 获得 SMTP 250，并非送达；`partial` 仅部分收件人被接受且 DATA 250；`rejected` 是服务器明确拒绝；`failed_before_data` 尚未发送正文；`unknown` 进入 DATA 后断线，无法判断是否接受；`sending` 可能发送中，也可能已中断。

SQLite 条件更新在联网前持久化 `sending`，共享私人目录的进程也只能尝试一次。回执保存失败返回 `unknown`，持久状态仍阻止重发。同一草稿重复调用仅查询既有状态，不连接 SMTP。尚未尝试的草稿遇冷却保持 `ready`，返回等待秒数。进程崩溃可能留下 `sending`，没有强制解锁入口，须人工核实。

SMTP 连接至少间隔 2 秒，沿用跨进程失败退避；认证只尝试一个机制，失败至少冷却 60 秒。SMTP 与 IMAP 分开限速，不占用验证码收信通道；无自动重试。

`mail-outbox.sqlite3` 仅存随机编号、状态和 DPAPI 密文。地址、正文、MIME、附件字节及回执均在密文中，密码不入发件箱。默认位于忽略的 `.local`，额外忽略同名数据库及 journal 文件。当前发送草稿仅支持 Windows 当前用户；其他平台既有 IMAP 环境变量方式仍可用。没有明文降级、自动清理或跨账号迁移。

SMTP send 本身不保存服务器副本；有归档需求时使用 check/save_sent_copy。项目不请求送达/阅读回执、不自动回复、不做群发任务。内容摘要不是授权，调用代理必须依据用户请求判断发送权限。

## 已发送副本

真实测试账号的 SMTP 未自动生成已发送副本，归档工具已验证可补存。工具按 IMAP `\\Sent` 标志识别目标文件夹；缺失或多候选时返回 `needs_mailbox`，由用户选择现有可选文件夹，不猜名称、不创建文件夹。归档前进行 Message-ID 精确匹配，最多核对 20 个候选；截断不等于缺失。副本返回必要 UID/UIDVALIDITY，不默认读取正文。已有多个副本只报告，不删除。

只有明确 `accepted`/`partial` 的草稿能保存；发送未知不允许归档。固定 MIME 字节校验通过后，在同一 SQLite 中独立持久化归档门闩，再执行一次 APPEND，写入副本的 `\\Seen` 标记。目标文件夹加密保存，归档状态与发送状态独立；归档失败不导致重发 SMTP。自动路径沿用先前目标文件夹，跨进程也不能对同一草稿再次追加。

`saved` 是 APPEND 成功且查到；`saved_unverified` 是 APPEND 成功但查验未完成；`unknown` 是保存结果未知；`previous_attempt` 是本地已存在尝试。除首次 `missing` 外，这些状态不得触发再次 APPEND，只查验或人工处理。服务器延迟生成副本及其他客户端同时写入不受本地门闩控制，不能承诺整个账号绝无重复。删除本地数据库会丢失发送与归档的防重记录，不能作为恢复办法。

归档副本不能证明外部投递。附件从已发送副本下载并匹配 SHA256，仅验证学校存储与读取；必须有目标邮箱附回附件并再次校验，才能证明外部往返内容一致。

## 验证范围

隔离测试覆盖缺项、头注入、Bcc、附件快照、并发门闩、部分/全部拒收、认证冷却、DATA 断线、持久化失败、线程匹配及真实 Windows DPAPI。stdio 冒烟检查工具合同与注解，不发送真实邮件。实际发送仅针对用户指定对象另行执行，记录见 [validation.md](validation.md)。能力较弱模型尚未独立评测；固定分支和合同测试不能替代真实模型评测。
