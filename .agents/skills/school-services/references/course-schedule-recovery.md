# 课程安排与登录恢复

## 查某门课什么时候、在哪里上

使用 `school_jw_course_schedule`，AI 只提供范围参数，程序负责确定课堂、读取课表及同学期同班 BB 公告。不查询邮件，不更改课程。

1. `query` 填用户提供的名称或课程代码；`semester_id=0` 指当前学期，历史学期先用 `school_jw_list_semesters`。已知具体教学周时传 `week`；`weekday=6` 为周六。没有周次时用 0，不能自行把所有周次解释成最近一天。
2. 问实验/上机时间设 `focus="experiment"`。普通授课安排不能证明实验安排。默认 `include_bb=true`；用户仅查教务课表才设 false，并在回答中说明范围。
3. 已有使用保存身份登录的用户授权时传 `login_authorized=true`，无需重复询问；`wait_seconds` 为一次登录进度等待，范围 0–30 秒，不是整个查询的网络超时。
4. 按返回状态处理：

| state / 字段 | 操作 |
| --- | --- |
| `confirmed` | 只按 `timetable` 的原值报告；日期来自平台。`week=0` 只报教学周和星期。说明 `confirmation_scope`。 |
| `ambiguous` 且有 `candidates` | 把候选名称、教学班交给用户选择，用返回的 `lesson_id` 再查；不能选择第一项。 |
| `ambiguous` 且有 `announcements` | 核对带来源的有限原文；脚本没有把公告转换成最终课程安排。发布时间、作业截止时间、相对日期不能当上课时间，调整通知也不能直接覆盖所有周次。无法确定时把具体疑点交回用户。 |
| `insufficient_evidence` | 说明缺少哪一源，不回答“没有课”。保留已取得的课表作为部分证据。 |
| `partial=true` | 来源失败或公告截断，不能宣称检查完整。按 `sources` 指示恢复或明确未核实。 |
| `sources.bb.state=course_mapping_required` | 缺少同学期同班标识；候选仅供核对，不能自动换成去年课程或另一个班。可根据用户确认的 BB 课程 ID 单独读取公告并注明人工映射。 |

同一工作流每站最多一次登录恢复。不同名称的课程不因都有“AI”“模型”等词而视为同一门。BB 当前仅覆盖门户提供的课程及可读公告页面，不保证公告分页、附件和其他通知渠道完整；不自动下载或执行附件。

## 六站统一只读查询恢复

`school_jw_query`、`school_bb_query`、`school_library_query`、`school_nan7_query`、`school_young_query`、`school_finance_query` 支持按枚举选择 `operation`，具体 `parameters` 见工具描述中的函数签名。不要猜参数或把写操作塞进读取接口。

例如：`school_bb_query(operation="announcements", parameters={"course_id":"从课程列表得到的ID"}, login_authorized=true)`。成功返回 `completed=true` 和 `data`，表示真正重新读取了原业务，不是仅发现旧的 connected 状态。

| state | 下一步 |
| --- | --- |
| `authentication_required` | 先核对已有授权；已授权保存身份登录可传 true 重查，没有则说明需要授权。 |
| `login_running` | 按 `poll_after_seconds` 查 `status_tool`，完成后重做原查询；仍在运行不重复启动。 |
| `login_busy` | 另一站点登录占用共享身份，等它完成。 |
| `manual_verification_required` / `identity_required` | 需要本人在本机完成验证或配置；不索取密码到对话，不自动弹窗。 |
| `cooldown` | 等 `retry_after_seconds` 后再查，不换工具或重连绕过。 |
| `access_denied` | 权限不足，停止恢复登录。 |
| `authentication_failed` / `login_failed` | 本次恢复失败，停止自动循环，检查 `status_tool`。 |
| `unavailable` / `local_policy_unavailable` | 查询或本地策略异常，不推断认证失效，也不能当作空结果。 |
| `invalid_arguments` | 根据工具描述修正参数。 |

发送、提交、报名、发布和选退课继续使用各自的准备/执行/核验流程。本封装不重放这些操作。邮件验证码仅沿用本人此前启用的本机许可，不代替新的授权。
