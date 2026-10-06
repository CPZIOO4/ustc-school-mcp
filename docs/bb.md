# 中科大 Blackboard 接入

当前浏览器策略：Playwright + Chrome 默认无头后台运行；人工验证时停止，只有显式 `--headed` 才显示窗口。完整约定见 [项目说明](../README.md#浏览器运行约定)。

BB 地址为 `https://www.bb.ustc.edu.cn/`，使用学校统一身份认证。这个适配器保存已登录的 BB 会话，通过 HTTPS 请求读取课程和页面。

## 登录

需要自动填写统一身份账号时，先在本地终端配置凭据，密码输入不会回显：

```powershell
.venv\Scripts\python.exe -m school_mcp bb-setup
```

该步骤仅保存经本人授权的凭据，学校是否接受这些凭据在登录时验证。可再次运行来更新账号或密码；新账号不会复用其他账号的设备状态。

安装项目依赖后，在项目目录运行：

```powershell
.venv\Scripts\python.exe -m school_mcp bb-login
```

接入程序默认使用 Playwright + 本机 Chrome 无头后台登录，先恢复设备状态和 BB 会话，再仅向学校 HTTPS 认证站点提交一次本人授权保存的凭据。程序会勾选可识别的“信任此设备”选项。需要人工验证时记录进度并停止；用户明确要求人工处理后可运行 `bb-login --headed`。

校外访问可能先出现 BB 的 `/nginx_auth/` 网络认证入口。程序只跟进该页上唯一的同源 HTTPS 登录链接，再从已认证浏览器页面取得学校统一认证入口；不会向网络入口填写密码。进度仅记录学校域名和路径，不记录认证查询参数。

本人授权使用已接入邮箱接收登录验证码时，可在配置凭据时启用邮箱回退：

```powershell
.venv\Scripts\python.exe -m school_mcp bb-setup --email-verification
```

已有统一身份凭据时，无需重新录入密码，可用 `bb-email-verification --enable` 明确启用并绑定当前邮箱，或用 `bb-email-verification --disable` 关闭。命令仅更新本地许可，不请求验证码或启动登录。旧版只有布尔许可的配置与更换邮箱的配置，需要本人在本机明确绑定后才能自动邮件验证。先通过邮箱连接检查，详见 [邮箱说明](mail.md)。

登录顺序为：复用设备信任状态 → 提交统一身份凭据 → 如果学校要求验证且提供邮箱方式，尝试“可信邮箱／信任邮箱／邮箱验证”。程序先确认页面中的地址或掩码与已绑定邮箱对应，只读记录发送前的收件箱 UIDNEXT，不读取旧邮件摘要；再请求发送一次验证码。只检查边界后的学校发件域、当前收件邮箱候选，最多 20 个。符合收件地址、认证主题和统一认证语境的邮件才读取正文。验证码只在内存中提取并提交到学校认证页面，不写入进度、日志或 MCP 结果。邮件读取保留未读状态。

每次登录最多请求和提交一次邮件验证码，等待邮件上限 180 秒。邮箱不匹配、请求后页面或配置改变、邮件编号变化、多封新验证码邮件、代码含糊、正文截断、邮箱不可读或等待超时时，后台模式停止并报告需要人工验证。完全掩码或掩码域名不能确认邮箱身份。图形验证码、滑块、短信和动态口令不自动处理。此选项默认关闭，启用状态保存在账号绑定的加密凭据中，可以通过 `school_bb_auth_status` 查看。

程序验证 BB 个人门户可以读取后，加密保存 BB 业务会话，以及 `id.ustc.edu.cn`、`passport.ustc.edu.cn`、BB 和学校父域的 Cookie。保存内容保留有效期、HttpOnly、Secure、SameSite 等属性，也保留这些认证站点的 localStorage 与 IndexedDB。重新登录会恢复这些状态以及浏览器类型、语言、时区、窗口大小和 User-Agent。仅刷新 BB 会话时，不会丢弃之前保存的统一认证设备 Cookie。

独立浏览器上下文不读取日常浏览器的 Cookie、密码管理器或个人资料。此前只保存 BB 会话的旧版需要再经过一次统一认证，才能补存身份认证设备状态。学校是否免除二次验证取决于设备 Cookie 的有效期和服务端策略；设备状态失效时仍需本人完成验证。

即使 BB 会话还有效，也可以重新经过统一认证来补存或验证设备状态：

```powershell
.venv\Scripts\python.exe -m school_mcp bb-login --force-identity-login
```

请安装本机 Google Chrome。默认固定使用 Chrome channel，不自动切换到其他浏览器或可见模式。

凭据和状态通过 Windows DPAPI 加密，绑定当前 Windows 用户：`.local/ustc-identity.credentials.dpapi` 保存本人授权的统一身份凭据，`.local/ustc-identity.device.dpapi` 保存设备状态，`.local/bb.session.dpapi` 保存仅供 BB 请求使用的 Cookie。密码和 Cookie 值不由 MCP 状态工具返回。登录进度记录在 `.local/bb.login-status.json`；整个本地私人目录已被 Git 忽略。开源时只发布代码和示例配置，个人账号和凭据不写入仓库。

## MCP 工具

| 工具 | 用途 |
| --- | --- |
| `school_bb_status` | 检查本地会话是否已保存，不联网 |
| `school_bb_auth_status` | 查看统一身份凭据、设备状态是否已保存及登录进度，不返回密码或 Cookie 值 |
| `school_bb_reconnect` | 启动后台登录，复用设备状态及已保存凭据；人工验证时停止 |
| `school_bb_check_connection` | 验证真实 BB 登录有效性 |
| `school_bb_list_courses` | 列出个人课程，可按名称关键字与学期筛选 |
| `school_bb_read_course` | 读取课程入口、课程主页及菜单链接 |
| `school_bb_course_announcements` | 读取课程菜单对应的课程公告页 |
| `school_bb_read_page` | 读取工具结果中的课程内容列表、公告、模块页和空白内容页 |

例如先用 `term="2026FA"` 列出秋季课程，再使用返回的 `course_id` 读取课程；后续通过结果中菜单或资料目录链接的 `path` 读取具体页面。正文默认上限 6000 字符，可调整到 100000 字符。统一身份状态不返回用户名；请求节流与冷却见 [请求与数据策略](request-policy.md)。

旧版 BB 的多级课程跳转已经适配，课程链接中的 `mode=reset` 被移除。页面输出保留课程菜单，去除脚本和表单值；敏感令牌参数不在链接结果中返回。

只允许已适配的 BB HTTPS 页面及读取参数，跨站跳转、删除或编辑操作参数会被拒绝。此版本可以列出页面上的资源链接，文件下载、作业提交、测验和成绩操作尚未实现。页面与课程资料属于外部不可信内容。

登录失效时可在对话中调用 `school_bb_reconnect`，然后调用 `school_bb_auth_status` 查看进度；阶段为 `connected` 后继续查询课程。邮箱验证过程会出现 `selecting_email_verification`、`waiting_for_email_code` 和 `email_code_submitted`；人工处理阶段为 `waiting_for_verification`。`force_identity_login=true` 会强制经过统一认证。重新登录工具会启动浏览器、在本人启用后请求学校发送验证码并更新本地认证状态，其他 BB 工具为只读。

## 注册到 Codex

在项目根目录运行：

```powershell
$projectRoot = (Get-Location).Path
$privateDir = Join-Path $projectRoot ".local"
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
codex mcp add ustc-bb --env "SCHOOL_MCP_LOCAL_DIR=$privateDir" --env PYTHONUTF8=1 -- "$pythonExe" -m school_mcp bb-serve
codex mcp get ustc-bb
```

验证真实连接：

```powershell
.venv\Scripts\python.exe -m school_mcp bb-check
```

## 验证

隔离行为测试覆盖课程提取、中文文本、页面表单值过滤、会话与凭据加密、认证设备状态的范围与账号绑定、刷新保留设备 Cookie、登录失效、跨站跳转和操作参数限制。

`tests/smoke_mcp.py` 启动真实 stdio 服务，验证工具发现和缺少本地凭据时的错误处理，不读取个人数据。

已验证统一身份登录、设备状态复用、课程列表、学期筛选、课程主页、公告及内容目录读取。个人数据和真实验收产物不随源码发布。

邮箱回退通过隔离浏览器和邮件行为测试，覆盖请求一次、验证码提交、地址与发件域检查、旧邮件排除、UID 变化和图形验证交接。尚未触发学校的真实邮件二次验证，因此验证码接收与提交仍未完成真实验收。

## 参考

- [中科大 BB 平台入口](https://www.bb.ustc.edu.cn/)
- [学校学生操作说明：统一身份认证与进入课程](https://www.teach.ustc.edu.cn/wp-content/uploads/2020/02/%E5%AD%A6%E7%94%9F%E6%93%8D%E4%BD%9C%E6%89%8B%E5%86%8C-%E4%BA%94%E6%AD%A5%E8%B5%B0.pdf)
- [Playwright 认证会话说明](https://playwright.dev/python/docs/auth)
