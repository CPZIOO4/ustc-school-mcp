# BB、教务、邮箱的固定后台流程

这些 MCP 工具把重复业务串成独立脚本：AI 提供参数，Python 读取冻结任务，在无头 Chrome 或既有 IMAP/SMTP/教务接口上执行。进程不调用模型，不需要对话保持运行。详细工具路线和参数见 [Skill](../.agents/skills/school-services/references/script-workflows.md)。

| 平台 | 新工具 | 范围 |
| --- | --- | --- |
| BB | prepare_named_submission、schedule_submission | 唯一定位作业、冻结材料；授权后定时提交，核验新尝试与附件；不明结果只核实 |
| 教务 | build_timetable | 自动读取已选课、分页找开课，按学分/冲突/空闲时段生成方案，默认保留已选课 |
| 教务 | schedule_enrollment_watch | 固定课堂的开放批次和余量监控；开放且余量满足后停止，满员继续监控；选退课写接口待真实开放批次验证 |
| 邮箱 | schedule_workflow | 固定草稿定时发送/可选发件副本、按线程等回复、限定邮件观察、规则自动归档 |
| 共用 | script_status、cancel_script | 加密任务状态、只读结果查询、停止后续动作；默认返回汇总 |

工具名加 `school_bb_`、`school_jw_` 或 `school_mail_` 前缀。完整 schema 可通过 MCP list_tools 获取。没有通用任意命令、URL、Python 或 JavaScript 执行入口。

## 实现与运行限制

- `script_jobs.py` 提供 DPAPI + SQLite 任务记录、唯一进程认领、写前保存原操作 ID、跨进程取消、账号绑定、有限只读重试和指数退避。私有数据库位于配置的 `.local/script-jobs.sqlite3`，忽略文件及 sidecar 不进入开源仓库。
- `script_workflows.py` 只组合已有业务接口。外部写入复用原有邮件/BB/归档的防重复门，不重发未知结果；归档在前一批确认成功后才能推进 UID 游标。
- 轮询至少60秒，另沿用每个平台的全局请求限速。任务必须有结束时间（最长30天）和处理条数上限；没有为了抢名额绕过平台限制的高频模式。
- 通过虚拟环境 `pythonw.exe -m school_mcp script-worker --job-id <id> --private-dir <private-directory>` 启动隐藏后台进程。Windows 设置 CREATE_BREAKAWAY_FROM_JOB，启动器退出后仍可运行；环境不允许独立进程则报告失败，不降级成会被父进程退出带走的任务。命令行只有本地任务编号和目录，凭据不进入参数。此命令为内部入口，不用它重启结果不明的任务。
- 关闭 Codex 后进程可继续；电脑必须保持开机、登录、联网且不睡眠。当前版本不注册开机任务，不保证重启后恢复。启动失败和进程中断可从状态工具识别。取消不撤销在途写入。
- 监控结果保存在本机，通过状态工具查看，不自动调用模型、发送通知邮件或桌面通知。邮箱观察只留统计与 UID 引用，不保留正文；归档及实际发送复用原有加密记录。
- 邮件认证继续由既有统一登录模块处理：用户启用授权后，只读取本次认证窗口的新学校认证邮件，代码不返回模型；不是通用的后台验证码读取接口。

## 验证边界

2026-10-08：后台进程与实际 stdio 工具链采用临时目录及合成草稿验证启动、重复请求和取消，未发送真实邮件。BB 提交、归档、回复、排课组合路径通过隔离测试；底层 BB 和邮件已验证能力的证据沿用各自文档。本轮教务登录恢复并实际读到 `window_closed`、`open_turns=[]`；未执行选课或退课。

真实 Coremail 不返回 UIDNEXT，已适配先 STATUS、再仅 UID SEARCH 的只读边界查询并复验增量扫描通过。真实当前学期排课读取流程返回 needs_input，未擅自补全缺少的数据或声称已生成有效课表；合成的完整课程数据可生成方案。青春科大仍沿用原任务参数，等待进程恢复后跨启动命令退出检查存活。

运行检查：

```powershell
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -p test_script_workflows.py -v
.venv/Scripts/python.exe -X utf8 tests/smoke_mcp.py
.venv/Scripts/python.exe -X utf8 tests/release_privacy_check.py
```
