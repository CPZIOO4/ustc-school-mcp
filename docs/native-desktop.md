# Rust 原生客户端迁移

桌面宿主使用 Rust + Tauri 2，复用现有蓝白界面。`native/core/` 负责模型请求与校验、上下文整理、任务与聊天加密记录、执行决策、固定工具目录、失败恢复规则、财务材料预检、共享限速、会话存储及公开查询。0.7.0 进一步接入模型配置/能力测试、候选配置切换、应用文件上传校验、对话中断恢复、BB 附件与提交执行、青春科大查询/报名监控/系统调度。BB 与青春科大的固定 DOM 流程通过内部 Rust CDP 驱动执行；0.8.0 将原生应用主后端、对话执行、授权与停止、官方人工登录移入 Rust；其余业务通过固定 Python 私有管道适配器执行。目标是全面迁移，实际进度见 [迁移进度](rust-migration.md)。

## 使用与数据

Windows 包为 `USTC-School-Agent-Native-Windows.zip`。完整解压后双击 `USTC-School-Agent.exe`；必须保留旁边的 `school-core.exe` 和 `engine` 文件夹。首次登录、模型 API 配置和学校业务入口与原应用相同。窗口使用系统 WebView2，不再为应用界面打开 Chrome。学校网页自动化默认后台 Chrome：已迁移的固定流程使用 Rust CDP，其余使用 Playwright；官方登录验证窗口按需要打开。

沿用 `%LOCALAPPDATA%/USTCSchoolAgent` 中的本人登录态、模型配置、对话、课表快照和订阅，不复制原始 MCP `.local`。已有引擎提供兼容 Rust 核心时复用；若仍是旧引擎，新窗口会提示先结束原任务并退出原应用，不强制结束它、不复制另一份业务数据库。关闭附加窗口不会停掉原引擎。明确点击“退出应用”会退出共享引擎。宿主自己启动引擎时，关窗先请求安全退出；业务运行中提示等待或停止任务。宿主崩溃后管道关闭会停止后续步骤，等待已发出的操作结束，再退出引擎；不会自动重放提交。

原有浏览器版包仍可使用，原加密数据和记录保留。青春科大任务库启用原生防重后，旧程序只可读取这些报名记录，继续报名须使用新版；不要清空数据库以绕过防重。其他数据的兼容范围见迁移文档。该阶段不能保证总安装体积更小：包内仍包含 Python、OCR 和 Playwright 依赖。

0.7.0 新建青春科大原生计划由 Windows 任务调度器直接启动 `school-core.exe --young-worker`；任务注册无法核验时，只能在明确核对后使用本机后台 Rust 等待进程，该降级不跨电脑重启。计划绑定当前核心版本、SHA-256 和账号。旧 0.6 原生计划、核心文件改变或账号不符时须重新复核，不能由旧 Python 入口重放。会话失效需要在应用账号页重新登录；原生 worker 不自动回退 Python 登录。取消监控不等于撤销已完成的报名。

## 通信与边界

- 界面资源打入 Rust 程序，只允许本地应用页面调用两个受限命令；不给远程网站、任意 Shell、任意文件或通用 HTTP 代理权限。
- 本地引擎握手通过匿名管道传递，仅接受 `127.0.0.1` 动态端口。能力凭证保留在 Rust 内存，不写入页面 URL、sessionStorage 或原生日志。
- 请求按方法、路由、查询键和大小校验，保留原后端登录与提交授权检查；不跟随重定向、不使用环境代理、不自动重试提交。
- 宿主使用受限 HTTP 客户端；本轮原生回环服务每次响应关闭连接，拒绝含糊或不完整请求，避免残留请求体污染下一次操作。模型网络客户端仍复用连接。
- 原生日志位于应用数据目录 `diagnostics/native-events.jsonl`，只记录时间与固定错误代码，不含网址、正文、凭证或原始异常。该日志目前与业务日志分别保存。
- 界面到宿主的资料上传使用二进制 IPC；Rust 后端按 1 MiB 分块调用存储校验，避免单条管道消息超过上限。导出保留原文件内容与聊天/文件权限边界。
- Rust 检查应用上传目录、分块偏移、文件长度和摘要，拒绝越界路径、符号链接与 Windows 重解析点；未完成的上传不能作为有效材料传入聊天。
- 模型配置和候选配置在 Rust 中加密保存。能力测试核对当前配置，成功前不将候选配置替换为正在使用的配置；失败保留原配置。应用启动后的中断恢复以事务标记原对话并保留业务回执，不自动重新执行工具。
- Rust CDP 只接受编译在业务模块中的固定操作，各页面提供请求约束；模型不能调用通用页面执行接口。只管理本程序启动的 Chrome，不接管用户正在使用的浏览器。

## 开发与构建

需要 Rust stable/MSVC、Windows C++ Build Tools、WebView2、项目 Python 环境。依赖由两个 Rust 工程各自的 `Cargo.lock` 和 `uv.lock` 锁定。Rust 构建产物、临时资源和用户数据被 Git 忽略。

在仓库根目录执行：

```powershell
cargo test --locked --manifest-path native/Cargo.toml
cargo test --locked --manifest-path native/core/Cargo.toml
cargo build --locked --manifest-path native/core/Cargo.toml
cargo run --locked --manifest-path native/Cargo.toml
powershell -File packaging/build-native.ps1
```

源码调试模式可以启动仓库 `.venv/Scripts/python.exe`；发布构建只运行随包引擎，不回退到任意系统 Python。构建脚本沿用 `school-agent.spec` 的资源白名单，输出独立原生 ZIP，不打包工作目录或真实用户资料。不需要安装 Node.js 来构建这个阶段的原生客户端。

验收入口 `tests/smoke_native.py --exe <原生 EXE>` 使用后台 WebView2 与 Playwright CDP，隔离合成数据，检查真实 Rust IPC、模型工具循环、上传下载、课表、资料和锁定。调试端口只由测试进程环境启用，普通程序不会自动开放调试端口。

## 后续平台与迁移范围

| 部分 | 当前实现 | 后续工作 |
| --- | --- | --- |
| 窗口、接口桥接、进程管理 | Rust + Tauri | 各平台构建与生命周期验收 |
| 界面 | 共用 HTML/CSS/JavaScript | 按实际需要逐步加 TypeScript |
| 模型请求、上下文、执行决策、任务授权与防重、工具目录、失败恢复、财务预检 | Rust 核心 | 保持原任务与回执兼容 |
| 模型配置、能力测试、候选切换、上传目录与文件完整性、中断恢复 | Rust，已接入应用实际入口 | HTTP 路由、通知和对话调度已在 Rust；剩余业务入口继续迁移 |
| 其余站点适配、材料解析/OCR和订阅等后台任务 | Python 临时兼容层 | 逐模块迁移并验证，最终移除 Python |
| 应用配置与聊天安全存储 | Rust 调用原 Windows DPAPI | macOS Keychain、移动端安全存储等独立实现 |
| 站点会话与信任设备 | Rust 校验、DPAPI、原子写和同文件跨进程锁 | 官方人工登录已在 Rust；邮箱绑定与自动邮件验证码回退待迁移 |
| 限速与公共站点 | Rust SQLite 策略、教务处/评课社区 HTTPS 与解析 | 其他网站逐站替换 |
| 教务 | Rust 认证查询、业务解析/决策和排课 | 查询顺序与入口封装仍有 Python；选退课写入未实现 |
| BB 提交 | Rust 附件读取/冻结/上传、兼容提交锁、固定 DOM 和回执核验 | 工具入口及通用脚本调度仍有 Python |
| 青春科大 | Rust DOM 查询、令牌捕获、报名执行、监控和 Windows 系统调度 | 官方人工登录已在 Rust；特殊报名流程未实现 |
| Chrome 生命周期与固定页面操作 | Rust 管理自有进程、内部 CDP；Windows Job Object 保留 | 其余网站 DOM 仍使用 Python Playwright；非 Windows 尚未实机验收 |
| Windows | 本轮构建与验收对象 | 安装器与签名按发布需求处理 |
| macOS/Linux | 尚未支持完整业务引擎 | 移除 DPAPI/msvcrt 等 Windows 依赖，再做原平台验证 |
| Android/iOS | 尚未提供可用安装包 | 确定移动业务执行位置、身份绑定与验证方式 |

Tauri 的跨平台能力不等于现有 Playwright 桌面自动化可直接在手机运行。手机端不能简单打包 Windows 引擎；本轮不默认上传凭证或建立云端代理。iOS 编译和签名还需要 macOS/Xcode 环境。

本轮 BB/青春科大的提交执行以合成学校页面和真实后台 Chrome 验证，未使用真实账号提交新业务，也未为验收注册真实报名计划。原生窗口、独立 MCP 服务启动和真实学校业务登录是不同的验收项目，具体证据见 [验证记录](validation.md)。不将语言迁移本身当作速度提升或无缺陷的证明。
