# 财务综合平台与智能报销

官方入口：[财务处](https://finance.ustc.edu.cn/) → [财务综合信息平台](https://cwzh.ustc.edu.cn/WFManager/home2.jsp) → 统一身份认证 → 智能报销。路线来自 [学校通知及操作指南](https://finance.ustc.edu.cn/2025/0403/c21813a679402/page.psp)，2026-10-08 实际验证门户与智能报销登录界面。

启动独立服务：`python -m school_mcp finance-serve`。使用与既有学校服务相同的私人目录和统一身份凭据；需要本机Chrome。默认无头、不出现桌面窗口。手动维护命令为 finance-login、finance-check；doctor支持 `--service finance`。

| 工具（统一前缀 school_finance_） | 范围 |
| --- | --- |
| entry_points | 已核实官网、门户及操作指南，不联网 |
| status | 本机配置与后台登录进度，不输出秘密 |
| reconnect | 复用授权凭据和信任设备后台登录 |
| check_connection | 验证财务门户和智能报销入口存在 |
| list_services | 当前门户的服务名称，不暴露单点跳转上下文 |
| inspect_smart | 后台验证智能报销登录界面及有限可见模块 |
| workflow_guide | 官方指南的5类业务、信息清单与填单路线，不代表实时权限 |

会话使用 Windows DPAPI，加密为私人目录中的 finance-session.dpapi，绑定当前统一身份账号。财务会话与其他站点隔离，只保留 cwzh.ustc.edu.cn 的 Cookie/Origin；认证设备状态沿用共同机制。旧CAS服务回调含HTTP时先拦截再以相同学校主机的HTTPS访问，不禁用TLS验证，不输出票据或JSESSIONID。

当前 `connected` 分为 financial_portal 和 authenticated_smart_shell。后者只确认登录界面；catalog_complete=false 表示动态菜单未完整适配，不表示无单据或无权限。仅放行已检查的初始化元数据读取，其他POST保持拦截。官方指南列出的各报销业务尚未逐项实现，不创建、保存、提交或审批报销单，不上传发票或支付。

弱模型操作分支见 [财务 Skill](../.agents/skills/school-services/references/finance-workflows.md)。后续按具体业务逐项适配，避免把门户全部隐藏配置菜单作为用户可办理功能。

workflow_guide支持overview、daily、travel、loan、remuneration、internal_transfer，返回2025年4月指南来源及PDF实际页码。报销事项、经费来源、金额依据和用户指定票据需先补齐；材料清单是文档辅助，不是完整审核标准。2026-10-08重新验证登录成功，动态菜单仍未完整加载，真实填单尚未验证；本轮未建立报销单。

可按现有README安装变量注册：

```powershell
codex mcp add ustc-finance --env "SCHOOL_MCP_LOCAL_DIR=$privateDir" --env PYTHONUTF8=1 -- "$pythonExe" -m school_mcp finance-serve
```

本轮同时修复后台登录PID重用：Windows进程创建时间晚于旧PID记录时，不再误认为旧登录仍在运行；不终止被重用编号对应的其他进程。无法获取进程身份时保守保留串行登录锁。
