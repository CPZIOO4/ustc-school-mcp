# 青春科大脚本监控与报名

固定操作由本地 Python 脚本执行；AI 负责传入授权范围和参数、解释结果。Playwright + 无头 Chrome 运行，无 Computer Use、微信操作或到点 AI 唤醒。小程序码识别未实现；用户可提供完整名称，也可授权按多个关键词筛选。

## MCP 流程

1. `school_young_schedule_registration` 接收 `opens_at`（带时区）、`exact_name` 或 `name_keywords`（二选一）、`authorized=true`。关键词必须全部命中，忽略空格、标点、大小写及全半角差异；另核对开放时间。只有唯一候选才自动报名，歧义返回候选列表。首次定位后锁定该项目 ID。
2. `monitor_from` 默认提前两分钟。工具保存加密报名任务并尝试安装 Windows 计划任务；失败时改为无窗口 Python 驻留等待进程，并明确返回实际调度方式。原先按时唤醒 AI 的方式不再使用。系统任务核验失败但已注册时，先禁用该任务再启用驻留进程，避免双重触发。
3. 启动时验证已有会话，必要时只恢复一次已有授权账号的后台登录。遇人工验证则停止。监控运行在独立脚本进程：刷新项目、定位目标、核对开放状态、提交一次、独立查询本人报名状态。`poll_seconds` 默认为 10 秒，是每轮查询完成后的最小普通等待间隔；分页和站点限速会增加总周期，非秒级抢名额保证。
4. `school_young_schedule_status(schedule_id)` 查询系统任务或驻留进程状态；`school_young_registration_status(job_id, verify=true)` 可独立核验不明提交。`school_young_cancel_schedule` 取消调度和未提交监控，不退掉学校已接受的报名。

监控任务默认没有原先的 15 分钟过期限制：找不到目标或尚未开放继续等待；报名成功、平台报名期结束、用户取消，或出现歧义、附加信息、不可处理的错误时结束。可用 `stop_at` 显式指定截止时间。读请求限流按持久化冷却退避；一般读取中断最多重试三次；写请求永不重发。

## 本机条件

Windows 系统任务使用当前登录用户、低权限及 `pythonw.exe`，不保存密码。系统拒绝安装时使用驻留等待进程；这时可以关闭 Codex，但电脑需要保持开机、登录及联网，睡眠期间不会执行，重启或注销会中断进程。实际模式、进程是否存活、开始时间与是否跨重启均由状态工具返回，不能把参数保存成功当作调度已就绪。

```powershell
python -m school_mcp young-registration-worker --schedule-id <计划ID> --private-dir <私人目录>
python -m school_mcp young-registration-status --job-id <报名任务ID>
```

`young-registration-run` 仍是手动执行一次的旧入口，不负责持续监控；定时使用 worker。创建后的 schedule_id 和 job_id 分别用于调度和报名状态查询。

## 业务边界与验证

目前只适配普通单次线下、无需附加资料的活动。系列、作品、线上活动、马拉松专用流程、未知字段、额外资格/费用/承诺停止；不可取消条件需要用户另有明确接受后设置 `allow_non_cancellable=true`。任务绑定当前账号，内容与回执 DPAPI 加密；写入前通过 SQLite 事务持久化 `submitting`。进程中断、响应不明时仅核验、不重发。报名成功不代表签到、实际参与或学时到账。

隔离测试覆盖匹配、时间、并发、轮询等待、取消、退避、一次提交和核验；本地合成页面测试使用真实无头 Chrome。真实会话和列表已读验证；实际报名仍待用户活动开放后验收。
