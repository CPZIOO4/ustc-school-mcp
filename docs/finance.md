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
| preflight | 离线检查材料摘要、已确认信息、金额差异和本次材料中的重复发票，返回缺项与固定下一步 |

会话使用 Windows DPAPI，加密为私人目录中的 finance-session.dpapi，绑定当前统一身份账号。财务会话与其他站点隔离，只保留 cwzh.ustc.edu.cn 的 Cookie/Origin；认证设备状态沿用共同机制。旧CAS服务回调含HTTP时先拦截再以相同学校主机的HTTPS访问，不禁用TLS验证，不输出票据或JSESSIONID。

当前 `scope` 区分 `financial_portal`（门户）、`authenticated_smart_shell`（智能报销登录）和 `authenticated_smart_catalog`（本次账号业务目录查询完整）。`catalog_complete` 只描述目录查询，不能代表整个网页或业务办理已验收。`page_initialization_complete=false` 表示首页不透明初始化过程仍被拦截。官方指南列出的各报销业务尚未逐项实现，不创建、保存、提交或审批报销单，不上传发票或支付。

弱模型操作分支见 [财务 Skill](../.agents/skills/school-services/references/finance-workflows.md)。后续按具体业务逐项适配，避免把门户全部隐藏配置菜单作为用户可办理功能。

## 离线材料预检

`school_finance_preflight` 为模型提供固定检查流程：整理用户提供的摘要 → 计算与检查 → 按缺项补充 → 再次预检 → 人工核对实时表单。它不登录、不读取文件或 OCR、不保存输入，不需要先连接财务处。支持日常报销、国内差旅、借款、薪酬、校内转账五类准备路线，材料清单以已有指南为辅助，不能替代当前学校要求。

工具接收一个 `request` 对象。以下为**合成示例**，不可复制用于真实单据：

```json
{
  "request": {
    "business_type": "daily",
    "facts": [
      {"field": "purpose", "value": "合成示例：办公用品", "confirmed": true},
      {"field": "funding_source", "value": "合成项目，使用依据已核对", "confirmed": true},
      {"field": "contact", "value": "已在本地核对", "confirmed": true},
      {"field": "settlement", "value": "已在本地核对", "confirmed": true}
    ],
    "contract_required": false,
    "claim_amount": "100.00",
    "lines": [{
      "line_id": "line1", "kind": "invoice", "amount": "100.00",
      "document_amount": "120.00", "document_ids": ["doc1"], "confirmed": true
    }],
    "documents": [{
      "document_id": "doc1", "kind": "invoice", "available": true,
      "confirmed": true, "status": "normal", "dedup_key": "SYNTHETIC-INVOICE-001"
    }]
  }
}
```

所有 `confirmed` 默认 false，只能对实际核对的信息置 true；未知金额为 null 或不传。金额必须为最多两位小数的非负十进制字符串，零金额会返回问题；不接受浮点数、负数或科学计数法。本版只检查人民币，不换算外币。模型应保留用户给出的拟申请金额与票面金额，支持低于票面的部分申请，但重复引用同一票据或分摊情况需另行核对，不自动拆票。

`business_type="auto"` 仅提供关键词候选，必须显式选择类型后才可通过；关键词不覆盖已选类型。`facts` 的字段及各类金额行由工具 JSON schema 限定，未适配字段或操作会被拒绝。

返回 `state=needs_input` 时，根据 `issues` 补齐并重跑。检查包括缺失/未确认信息、金额总和与票面上限、材料关联、缺少发票查重标识、同票重复引用、未知/作废/红字票据状态及分业务材料。`observed_line_totals` 是输入金额之和，可能仍含重复或不合法项目；不会自行扣减。`claim_minus_lines` 是拟申请总额减明细之和，缺金额或混合币种时为 null。

`ready_for_manual_review` 仅表示本次摘要没有触发这些检查，下一步仍是人工核对实时表单与学校要求。`safe_to_submit`、`totals_are_approved`、`invoice_authenticity_verified` 始终为 false。缺票据查重标识时 `duplicate_check.identities_complete=false`；查重范围仅限本次输入，`history_checked=false`，未验证票据真伪或是否已在别处报销。

返回值不包含事实原文或发票号码，只保留局部材料 ID、检查结果与金额摘要。不要把完整银行卡、身份证或真实票据标识放进公开示例。真实填写、保存、上传与提交仍未实现。

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
