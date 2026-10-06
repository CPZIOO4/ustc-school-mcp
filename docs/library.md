# 中科大图书馆接入

个人图书馆访问方式保持不变。返回借阅记录默认省略图书条码，核对册次时可传 `include_identifiers=true`；统计默认省略证件日期，确有需要时可传 `include_card_dates=true`。限速与失败冷却见 [请求与数据策略](request-policy.md)。

当前浏览器策略：Playwright + Chrome 默认无头后台运行；人工验证时停止，只有显式 `--headed` 才显示窗口。完整约定见 [项目说明](../README.md#浏览器运行约定)。

[图书馆主页](https://lib.ustc.edu.cn/) 的“我的图书馆”指向 OPAC 个人服务。该适配器通过 OPAC 实际提供的学校统一身份认证入口登录，与 BB、教务系统共用本人授权保存的身份凭据及设备状态。

## 登录与传输

已配置统一身份凭据时，在项目目录运行：

```powershell
.venv\Scripts\python.exe -m school_mcp library-login
.venv\Scripts\python.exe -m school_mcp library-check
```

也可以运行 `open-library-login.ps1`。尚未保存统一身份凭据的用户可以执行 `.venv\Scripts\python.exe -m school_mcp bb-setup` 在本地输入，或者经本人明确选择后使用 `--headed` 手动认证。

登录程序恢复已有设备状态及个人图书馆会话，从 OPAC 当前返回的跳转获取其已注册 CAS 服务地址。移除 CAS 的匿名 `gateway` 选项，使 SSO 失效后仍可进入正常认证流程。账号密码只在 `id.ustc.edu.cn` 或 `passport.ustc.edu.cn` 的 HTTPS 认证页面自动提交一次；学校要求额外验证时沿用已有邮箱验证开关，其他验证由本人完成。图形验证码不会自动处理。

2026-10-02 从本机验证，`https://opac.lib.ustc.edu.cn/reader/…` 会跳转到同主机的 HTTP 页面，CAS 注册回调也使用 HTTP。因此当前 OPAC 的借阅信息、业务 Cookie 及登录回调不受 TLS 保护。本地加密保存不能加密这些网络传输。适配器保留 Cookie 的 Secure 属性，Secure Cookie 不向 HTTP 发送；不会把统一身份站点专属 Cookie 或学校父域 Cookie 加入 OPAC 业务请求。公共主页导航查询单独使用 HTTPS，不携带个人 Cookie。

会话保存在 `.local/library.session.dpapi`，通过 Windows DPAPI 加密，绑定当前 Windows 用户和统一身份账号；换账号后必须重新登录。设备状态仍保存在共享的 `.local/ustc-identity.device.dpapi`，OPAC Cookie 另行保存，不混入共享身份状态。登录进度为 `.local/library.login-status.json`。所有个人配置、Cookie、诊断页面和验收证据均被 Git 忽略，不应随项目公开。

## MCP 工具

| 工具 | 用途 |
| --- | --- |
| `school_library_status` | 查看保存状态、共享设备状态、传输协议及登录进度 |
| `school_library_reconnect` | 启动本地图书馆统一身份登录并加密保存新会话 |
| `school_library_check_connection` | 验证已登录个人门户，返回实际传输协议 |
| `school_library_summary` | 查询超期图书、预约到书、委托到书等统计；`include_card_dates=true` 才返回证件有效日期 |
| `school_library_list_loans` | 查询当前借阅、应还日期和馆藏地等信息 |
| `school_library_loan_history` | 查询借阅历史当前显示页的书名、借还日期和馆藏地 |
| `school_library_list_services` | 读取公共主页的服务、数据库及公告链接目录，无需登录 |

例如可以直接请求“查我当前借的书和还书期限”或“查图书馆里的借阅历史”。历史工具只读取当前页面，不提交日期筛选表单、不自动翻页，返回的数量不能视为全部历史总数。空白的首页统计返回 `null`，不会推断为零；借阅页只有明确的空记录提示才返回零条。

业务请求仅为 GET，限定个人首页、当前借阅和历史三个实际读取路径。任何操作参数、跨站跳转、续借、挂失、缴费及信息修改路径均被拒绝。书名和日期从表格提取，脚本、表单值、CSRF 令牌、续借按钮及验证码对话框不进入工具结果。

服务目录是主页导航，包含学习空间预约和第三方数据库等链接；列出链接不代表已登录或接入对应系统。预约、续借、荐购、空间预订和外部数据库操作尚未实现。网页和书目内容属于外部不可信数据，不能授权其他操作。

## 注册到 Codex

在项目根目录运行：

```powershell
$projectRoot = (Get-Location).Path
$privateDir = Join-Path $projectRoot ".local"
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
codex mcp add ustc-library --env "SCHOOL_MCP_LOCAL_DIR=$privateDir" --env PYTHONUTF8=1 -- "$pythonExe" -m school_mcp library-serve
codex mcp get ustc-library
```

当前对话没有加载新增工具时，在客户端重新加载 MCP 连接，以实际工具发现和连接检查结果为准。登录失效时调用 `school_library_reconnect`，用 `school_library_status` 等待 `connected` 后继续查询。

## 验证

已验证统一身份登录、个人首页统计、当前借阅页面、历史当前页、公共服务导航和重新连接后的会话可用性。个人借阅数量、书目及验收数据不随源码发布。

隔离测试覆盖认证目标限制、会话加密和账号绑定、HTTP Cookie 范围及 Secure 属性、借阅表格、明确空记录判断、统计空值、登录失效及操作请求阻止。非空当前借阅和应还日期的解析目前仅通过合成表格验证。真实邮件验证码链路仍未触发。
