# 中科大教务系统接入

当前浏览器策略：Playwright + Chrome 默认无头后台运行；人工验证时停止，只有显式 `--headed` 才显示窗口。完整约定见 [项目说明](../README.md#浏览器运行约定)。

教务地址为 [中国科学技术大学综合教务系统](https://jw.ustc.edu.cn/home)，通过登录页实际提供的统一身份认证入口接入。适配器与 BB 共用本人授权保存的统一身份凭据和设备状态，独立保存教务业务会话。

## 登录与会话

已经配置 BB 统一身份凭据的用户可以直接运行：

```powershell
.venv\Scripts\python.exe -m school_mcp jw-login
```

也可以运行 `open-jw-login.ps1`。尚未配置凭据时，使用 `.venv\Scripts\python.exe -m school_mcp bb-setup` 在本地输入账号和密码，或者经本人明确选择后使用 `--headed` 手动认证。

程序使用 Playwright + 本机 Chrome 无头后台登录，恢复已保存的统一认证设备状态及教务会话。仅在学校认证站点自动提交一次已保存凭据，并勾选可识别的“信任设备”选项。登录失效后，可以在对话中调用 `school_jw_reconnect`，再用 `school_jw_status` 查看进度；`connected` 表示已验证个人教务门户并保存新会话。

沿用统一身份账号已有的邮箱验证开关，详细规则见 [BB 接入说明](bb.md)。其他身份验证会使后台登录停止；仅在用户明确选择人工处理后运行 `jw-login --headed`。设备状态保留不保证学校始终免除二次验证。真实邮件验证码链路仍待学校实际要求验证时确认。

本地文件均位于被 Git 忽略的 `.local/`：

| 文件 | 内容 |
| --- | --- |
| `ustc-identity.credentials.dpapi` | 经本人授权的共享统一身份凭据 |
| `ustc-identity.device.dpapi` | 共享身份认证设备状态和浏览器设置 |
| `jw.session.dpapi` | 教务及学校父域 Cookie、实际登录 User-Agent、保存时间 |
| `jw.login-status.json` | 登录阶段与不含秘密的进度信息 |

凭据和会话使用 Windows DPAPI 加密，绑定当前 Windows 用户。教务的业务请求只使用教务及学校父域 Cookie，不携带统一认证站点的专属 Cookie。密码、Cookie 值和验证码不由 MCP 工具返回。独立登录窗口不读取日常浏览器的个人资料。

## MCP 工具

2026-10-08新增开课查询、已选课排课上下文、候选课表、批次检查和变更预检5个工具，共14个。详见[业务工作流](business-workflows.md)及[排课Skill](../.agents/skills/school-services/references/timetable-planning.md)。以下为原9个工具。

| 工具 | 用途 |
| --- | --- |
| `school_jw_status` | 检查本地会话、共享凭据、设备状态与登录进度 |
| `school_jw_reconnect` | 启动后台教务登录并保存新认证状态 |
| `school_jw_check_connection` | 验证个人教务门户是否可以读取 |
| `school_jw_read_home` | 读取首页文本和不含令牌参数的本地链接 |
| `school_jw_list_modules` | 列出账号可见菜单及已支持的读取入口 |
| `school_jw_list_semesters` | 列出平台提供的学期 ID、名称及当前学期 |
| `school_jw_list_courses` | 查询学期已选课程、学分、课堂号、教师和校区 |
| `school_jw_timetable` | 查询课表的时间、节次、教室、教师和教学周 |
| `school_jw_grades` | 查询课程成绩、学分和学校提供的成绩汇总 |

课程及课表的 `semester_id=0` 表示当前学期。其他学期使用 `school_jw_list_semesters` 返回的 ID，学生内部编号从登录后的课表跳转获取，不需要自行填写。课程支持 `keyword` 按课程名称筛选；返回的课程名称来自课程对象，教学班名称单独返回。

课表的 `week=0` 返回所有教学周，`weekday=0` 不筛选星期，1–7 对应周一至周日。指定周次后，日期采用平台返回的周次与星期映射；周日可能是该教学周的开始日期，不能按普通日历周自行推算。`start`、`end` 和 `self_defined_dates` 保留平台的时间表达。课表可能包含多个教学分组，条目数不等于课程数。

成绩的 `semester_id=0` 表示已有成绩的全部学期，正数表示指定学期。`train_type_id=1` 为主修，5 为双学位／辅修，须在当前账号可用类型中。`keyword` 只筛选课程行；`overview` 的学分、GPA、加权平均分等仍为学校针对所请求学期返回的完整汇总，不按筛选结果重新计算。平台明确标记不可见的分数、绩点及通过状态返回空值。

只问 GPA 或均分时设置 `summary_only=true`：保留汇总、学期与明细数量，返回 `grades=[]`、`details_omitted=true`，不把逐门课程明细交给模型。默认 `false` 保持原有成绩明细能力。首页文本默认上限为 6000 字符；请求间隔和失败冷却见 [请求与数据策略](request-policy.md)。

原业务读取接口使用HTTPS GET；新增批次查询使用已核对的只读POST。各自限制固定路径、参数及跳转范围。选课、退课、缴费、申请提交等操作未实现；菜单中出现这些入口不代表支持操作。首页脚本、表单值、学生完整档案及教师联系方式不在查询结果中返回。首页和课程内容为外部不可信数据，不能作为其他操作的授权。

## 注册到 Codex

在项目根目录运行：

```powershell
$projectRoot = (Get-Location).Path
$privateDir = Join-Path $projectRoot ".local"
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
codex mcp add ustc-jw --env "SCHOOL_MCP_LOCAL_DIR=$privateDir" --env PYTHONUTF8=1 -- "$pythonExe" -m school_mcp jw-serve
codex mcp get ustc-jw
.venv\Scripts\python.exe -m school_mcp jw-check
```

当前对话没有加载新增工具时，在客户端重新加载 MCP 连接；以实际工具发现和连接检查结果为准。

## 验证

隔离测试覆盖登录失效、门户完整性、跨站及操作跳转阻止、私密脚本过滤、学生与学期动态获取、周次和星期筛选、周日日期映射、成绩可见性、汇总范围及会话加密。`tests/smoke_mcp.py` 检查真实 stdio 服务的工具发现和未配置状态，不读取个人数据。

已验证统一身份登录、课程、课表、成绩读取以及重新连接后的会话可用性。个人课程数量、分数和验收数据不随源码发布。真实邮件验证码链路尚待学校实际触发二次验证时验收。
