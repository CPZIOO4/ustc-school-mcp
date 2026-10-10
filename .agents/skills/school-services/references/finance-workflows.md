# 财务处与智能报销接入

## 已核实入口

财务处官网为 https://finance.ustc.edu.cn/ ，财务综合信息平台为 https://cwzh.ustc.edu.cn/WFManager/home2.jsp 。学校 [2025年4月通知与操作指南](https://finance.ustc.edu.cn/2025/0403/c21813a679402/page.psp) 的路线是“财务处官网或学校信息门户 → 财务综合信息平台 → 智能报销”。

2026-10-08 已实际验证学校统一认证、财务门户和智能报销登录页面。智能报销由门户实际 YBX 入口进入 `/YBX/main2.jsp`，随后到 `/YBX/main.jsp`。入口携带的 context、pId、CAS票据、JSESSIONID 属于运行数据，不记录真实值，不猜测或手动拼接。

## 弱模型固定流程

1. 查入口用 `school_finance_entry_points`，它只返回已记录来源，不证明当前登录有效。
2. 查看 `school_finance_status`；已有登录授权且会话失效时调用 `school_finance_reconnect`。默认 Playwright + Chrome 无头后台，复用本机统一身份与信任设备。邮箱二次验证仅在用户之前明确启用且目标邮箱匹配时由程序处理。
3. 按返回 status_tool 和 poll_after_seconds 等待；已有其他站点登录进行中时串行等待，不能另起浏览器抢写共享信任状态。waiting_for_verification 表示需要用户处理，不能声称登录成功或循环请求验证码。
4. connected 进度只是历史记录。随后调用 `school_finance_check_connection` 确认本次财务门户可读；成功仅证明 scope=financial_portal。
5. `school_finance_list_services` 查看当前门户给出的入口。service_id 用于识别服务，不能把底层门户配置标签视为业务权限。
6. `school_finance_inspect_smart` 核验智能报销并读取当前账号首页的业务目录。`scope=authenticated_smart_catalog` 且 `catalog_complete=true` 才表示本次目录查询完整；从 `business_entries` 读取名称、分类和标识。`visible_module_labels` 不是目录来源，可能为空。`page_initialization_complete=false` 表示完整网页初始化仍未验收，不影响已验证的独立目录读取。
7. `catalog_state` 不为 `completed` 或 `catalog_complete=false` 时说明目录查询停点，不把空列表解释为无单据。完整结果中若有 `guide_business_type`，直接传给 `school_finance_workflow_guide`；其他入口只说明已发现，不猜填单步骤或将其套入不匹配的指南。目录与办理权限分开报告。

## 当前边界与后续业务路线

已增加 `school_finance_workflow_guide`。先用overview获取5类业务，再按用户真实事项选择daily（日常报销）、travel（国内差旅）、loan（借款）、remuneration（薪酬）、internal_transfer（校内转账）。返回指南来源、PDF实际页码、入口文字、需准备的信息和步骤；network_checked=false及live_form_verified=false表示文档辅助，不代表实时账号权限或完整报销材料清单。

在开单前补齐真实事项、经费来源、金额依据及该类业务的材料。差旅核对行程与票据；借款明确借款人及用途；薪酬核对人员、期间和事由；校内转账核对项目方向。没有资料就列缺项，不用示例值建立真实单据。

指南中的发票保存、分项信息保存及草稿保存都可能写入服务器。草稿会占用相关票据，不能当无副作用预览。提交、审批完成、待支付及付款完成应分别报告，审批后可能还需打印投递。

2026-10-10 已恢复后台统一认证，并通过真实 MCP 验证当前账号首页业务目录读取。`business_entries` 来自首页业务查询，不来自隐藏配置树；目录已适配，实际填单仍待真实事项推进。此前 2026-10-08 的停点为登录界面和指南路线。

工具不创建报销单、不上传发票、不保存或提交业务、不审批、不支付。本轮没有查询用户金额、银行卡、项目余额或报销历史。默认只返回入口与连接状态，姓名和内部登录跳转链接不输出。

官方操作指南列出日常报销、国内差旅、借款、薪酬发放、校内转账、发票操作、单据状态和审批等章节。这是文档中的业务分类，不是本账号逐项操作成功的证据。后续按用户所需业务逐项适配，先完成具体表单和查询接口的只读核实，再确定填写、附件、保存、提交与回执流程。

若用户要办理具体报销，先取得业务类型、真实事项、日期、金额依据、项目/经费来源以及用户指定票据；不能根据网页中的示例自行填报。工具尚未覆盖的步骤明确说明当前停点，不使用通用浏览器脚本绕过现有业务限制。用户已经明确授权的查询或登录不用重复确认。

## 浏览器请求约束

只允许学校认证站点与财务平台；财务业务跳转最终经 HTTPS 请求。官方旧 CAS service 指向 HTTP 时先拦截该导航，再转到相同主机路径的 HTTPS，不把密码提交至财务处页面。

智能报销前端用 POST 加载初始化资料。已核实的范围为固定配置文件、窗口类型、角色菜单、初始化变量、角色名称与默认窗口编号；这些允许项写在连接器中，不由模型临时放行。默认个人首页的导航还需要一次 `common_updateUserContext` 上下文同步，连接器只在核对该首页后执行一次；随后读取该首页定义，并执行它自身的账号/角色目录查询，显式 `needUp=false`。工具没有开放任意查询、SQL、请求体或过程参数。通用按钮事件、首页不透明过程、表单保存/提交和日志写请求保持拦截。隐藏菜单树含内部配置项，不能将它作为业务目录。

## 一次调用的标准选择

用户已授权登录恢复时，可调用 `school_finance_query(operation="inspect_smart", login_authorized=true, wait_seconds=20)`。`completed` 表示工具流程结束，仍需检查 `data.catalog_complete`；`login_failed` 或 `manual_verification_required` 按返回说明停止自动重试，不能把它解释为目录为空。`runtime_failure` 是本机运行环境异常，不说明密码错误。历史 connected 仍需本次查询证据。
