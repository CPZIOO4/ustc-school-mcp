# 邮件整理与扩展收发

本模块增加13个工具，邮箱服务合计30个。标准调用步骤与状态分支见 [整理 Skill](../.agents/skills/school-services/references/mail-organization.md)。需要已有 IMAP 客户端密码配置；不额外取得账户权限。

| 能力 | MCP 工具 | 行为 |
| --- | --- | --- |
| 转发/回复全部 | prepare_forward / prepare_reply_all | 仅准备加密草稿，复用已有发送门闩 |
| 文件夹 | create_folder / list_folders | 明确创建或查看实际层级、is_archive |
| 已读、未读、移动、归档 | prepare_actions / execute_actions / action_status | 冻结选择、逐项核验、一次执行；状态查询可只读对账 |
| 撤销 | prepare_undo | 成功项的反向计划；状态改变则拒绝 |
| 导出 | export | 单封 EML/TXT/MD，多封 ZIP + 清单 |
| 分类 | get_classification_rules / save_classification_rules / preview_classification / prepare_classification | 已确认规则优先，未匹配可建议，冲突/认证邮件保留 |
| 往来 | read_thread | 有边界的线程头关联、原文与缺失信息供摘要 |

以上工具均带 school_mail_ 前缀。只有“归档文件夹”及其真实子目录算归档。已发送副本保存是独立功能，不计入此归档定义。

## 数据与执行保证

操作计划和分类规则保存在既有 mail-outbox.sqlite3 的独立表，使用 Windows DPAPI 加密。准备计划只读邮箱，固定账号、UIDVALIDITY、UID、内容摘要、原标记及目标目录编号；执行不重新搜索扩大范围。用户授权由调用代理依据上下文判断，摘要只绑定内容而不是授权凭证。

每个计划先持久化 running 门闩，逐项保存阶段。移动要求 UIDPLUS：UID COPY，检查 COPYUID，核验目标字节及标记，再 UID STORE 原 UID 的 Deleted 标记，最后 UID EXPUNGE 该 UID。不调用全局 EXPUNGE 或会隐式清理的 CLOSE。其他已删除邮件不受影响。未知结果阻止同一计划及重叠新计划再次操作；只读对账不解锁，因为原进程可能仍在运行。

部分失败不会自动回滚已经成功的项。撤销仅为完成/部分完成计划生成反向计划，核对操作后的原文与标记；之后被修改的邮件不会覆盖。复制响应丢失而缺少目标 UID 时需人工核对；不靠相同主题猜副本。移动回原位置也产生新 UID，不能承诺恢复旧 UID 或删除新建目录。

学校服务器只读能力探测确认 UIDPLUS；未广告 MOVE、CONDSTORE。前后核验能发现许多并发变化，但多条 IMAP 命令不是跨客户端原子事务。程序不承诺在其他客户端同时移动/修改同一封信时无竞争窗口。

## 限制

- 单封 MIME 最多20MiB，每批1–25封、总原文50MiB。转发和回复沿用10附件、20收件地址、正文100000字符及最终MIME20MiB上限，超限返回修正要求，不能静默截断。
- 转发默认保留完整可读正文和原附件；嵌套 message/rfc822 附件转换为可读 EML 文件（MIME重新序列化）；若需原始整封字节保真，请导出 EML。只回复全部不会自动附原附件。
- TXT/MD 导出不带附件字节，包含附件清单；EML 保留全部原字节。导出放私人 mail-exports 目录，Git忽略，不加载远程资源。原始EML仍是不可信邮件，程序不会执行。
- 分类按用户指令运行，无后台监控；类别需先确认，规则支持精确发件人/域名、主题包含。同优先级冲突保留，验证码/重置类优先保留。模型建议只可处理未匹配项，不能覆盖保护或扩展已确认类别。
- 线程工具最多5文件夹、25封、20次搜索、100候选头；默认正文每封2000字符。无线程头的邮件不会自动合并，截断或缺失会显示。只提供证据，摘要由代理生成，尚无独立弱模型成功率评测。

## 验证

隔离的有状态 IMAP 模拟器覆盖字节/标记保留、精确清除、重复执行、COPYUID错误、响应丢失、UIDVALIDITY变化、状态冲突、撤销、分类保护及导出内容。实际 stdio + Windows DPAPI 的合成流程由 `tests/smoke_mail_workflows.py` 验证。所有新邮箱写入均在合成服务中测试；尚未用真实邮箱验证移动、撤销、分类或新增转发/回复全部发送。详见 [验证记录](validation.md)。
