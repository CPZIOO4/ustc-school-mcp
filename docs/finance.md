# 财务综合平台与智能报销

官方入口：[财务处](https://finance.ustc.edu.cn/) → [财务综合信息平台](https://cwzh.ustc.edu.cn/WFManager/home2.jsp) → 统一身份认证 → 智能报销。路线来自 [学校通知及操作指南](https://finance.ustc.edu.cn/2025/0403/c21813a679402/page.psp)，2026-10-08 实际验证门户与智能报销登录界面。

启动独立服务：`python -m school_mcp finance-serve`。使用与既有学校服务相同的私人目录和统一身份凭据；需要本机Chrome。默认无头、不出现桌面窗口。手动维护命令为 finance-login、finance-check；doctor支持 `--service finance`。

| 工具（统一前缀 school_finance_） | 范围 |
| --- | --- |
| entry_points | 已核实官网、门户及操作指南，不联网 |
| status | 本机配置与后台登录进度，不输出秘密 |
| runtime_status | 离线检查当前进程版本与磁盘差异，更新后核对目标指纹；不验证学校登录 |
| reconnect | 复用授权凭据和信任设备后台登录 |
| check_connection | 验证财务门户和智能报销入口存在 |
| list_services | 当前门户的服务名称，不暴露单点跳转上下文 |
| inspect_smart | 后台验证智能报销登录，读取当前账号首页的业务入口目录及分类 |
| query | 固定查询并在已授权时恢复一次登录，之后继续原查询 |
| workflow_guide | 官方指南的5类业务、信息清单与填单路线，不代表实时权限 |

会话使用 Windows DPAPI，加密为私人目录中的 finance-session.dpapi，绑定当前统一身份账号。财务会话与其他站点隔离，只保留 cwzh.ustc.edu.cn 的 Cookie/Origin；认证设备状态沿用共同机制。旧CAS服务回调含HTTP时先拦截再以相同学校主机的HTTPS访问，不禁用TLS验证，不输出票据或JSESSIONID。

当前 `scope` 区分 `financial_portal`（门户）、`authenticated_smart_shell`（智能报销登录）和 `authenticated_smart_catalog`（本次账号业务目录查询完整）。`catalog_complete` 只描述目录查询，不能代表整个网页或业务办理已验收。`page_initialization_complete=false` 表示首页不透明初始化过程仍被拦截。官方指南列出的各报销业务尚未逐项实现，不创建、保存、提交或审批报销单，不上传发票或支付。

弱模型操作分支见 [财务 Skill](../.agents/skills/school-services/references/finance-workflows.md)。后续按具体业务逐项适配，避免把门户全部隐藏配置菜单作为用户可办理功能。

workflow_guide支持overview、daily、travel、loan、remuneration、internal_transfer，返回2025年4月指南来源及PDF实际页码。报销事项、经费来源、金额依据和用户指定票据需先补齐；材料清单是文档辅助，不是完整审核标准。2026-10-08重新验证登录成功，动态菜单仍未完整加载，真实填单尚未验证；本轮未建立报销单。

可按现有README安装变量注册：

```powershell
codex mcp add ustc-finance --env "SCHOOL_MCP_LOCAL_DIR=$privateDir" --env PYTHONUTF8=1 -- "$pythonExe" -m school_mcp finance-serve
```

本轮同时修复后台登录PID重用：Windows进程创建时间晚于旧PID记录时，不再误认为旧登录仍在运行；不终止被重用编号对应的其他进程。无法获取进程身份时保守保留串行登录锁。

## 2026-10-10 连接恢复与业务目录

后台统一认证、门户查询及智能报销目录已通过真实 stdio MCP 验证。修正门户中隐藏“修改密码”输入框导致的认证误判；Playwright 驱动启动的非标准异常现在写入终止状态，避免死进程一直停留在 starting。此前驱动日志出现 JavaScript 内存分配失败，恢复时驱动与 Chrome 已可启动；没有把运行环境异常判为密码错误。

`inspect_smart` 返回 `business_entries`、`categories`、`business_entry_count`、`catalog_state` 和 `catalog_complete`。每项只包含业务标识、名称、分类和是否有对应的指南类型；不返回执行代码、查询表达式、跳转上下文、个人金额或单据。目录取自登录账号首页“业务大类”查询，不读取隐藏配置树。目录项目数依账号和时间变化，不在公开实现中固定为某个数量。

固定链路为：实际门户入口 → 默认个人首页标识 → 一次导航上下文同步 → 一次首页定义读取 → 首页自身的账号/角色目录查询（`needUp=false`）。连接器先核对默认首页和定义，再调用其查询；工具不接受任意 SQL、窗口号、请求体或过程参数。依赖响应先由拦截器核验，再交给网页，避免异步事件顺序导致误拦截。上下文同步仅用于这次导航，不是保存报销单。

`commonUpdate_responseButtonEvent`、`logRemark` 和未知业务请求仍被拦截。查询可取得目录，完整页面初始化及实际表单仍未验证；不能据目录断言拥有审批、付款或提交权限。目录为空仅表示这次查询无入口，不等于没有单据。缺项、返回结构变化、超时及超过 200 项时返回明确的不完整状态，不重复执行或扩大请求范围。
