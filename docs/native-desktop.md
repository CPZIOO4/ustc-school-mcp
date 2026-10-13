# Rust 原生客户端迁移

桌面宿主使用 Rust + Tauri 2，复用现有蓝白界面。`native/core/` 负责模型请求与校验、上下文整理、任务与聊天加密记录、执行决策、固定工具目录、失败恢复规则、财务材料预检，以及共享限速、会话存储、Chrome 生命周期和首批公开站点查询。应用请求协调及多数学校网站业务适配尚在 Python 临时兼容层中；目标是全面迁移，实际进度见 [迁移进度](rust-migration.md)。

## 使用与数据

Windows 包为 `USTC-School-Agent-Native-Windows.zip`。完整解压后双击 `USTC-School-Agent.exe`；必须保留旁边的 `school-core.exe` 和 `engine` 文件夹。首次登录、模型 API 配置和学校业务入口与原应用相同。窗口使用系统 WebView2，不再为应用界面打开 Chrome。学校网页自动化仍默认后台 Playwright + Chrome；官方登录验证窗口按需要打开。

沿用 `%LOCALAPPDATA%/USTCSchoolAgent` 中的本人登录态、模型配置、对话、课表快照和订阅，不复制原始 MCP `.local`。已有引擎提供兼容 Rust 核心时复用；若仍是旧引擎，新窗口会提示先结束原任务并退出原应用，不强制结束它、不复制另一份业务数据库。关闭附加窗口不会停掉原引擎。明确点击“退出应用”会退出共享引擎。宿主自己启动引擎时，关窗先请求安全退出；业务运行中提示等待或停止任务。宿主崩溃后管道关闭会停止后续步骤，等待已发出的操作结束，再退出引擎；不会自动重放提交。

原有浏览器版包仍可使用，数据格式没有迁移或破坏性变更。若需要回退，退出原生应用后启动浏览器版即可。该阶段不能保证总安装体积更小：包内仍包含 Python、OCR 和 Playwright 依赖。

## 通信与边界

- 界面资源打入 Rust 程序，只允许本地应用页面调用两个受限命令；不给远程网站、任意 Shell、任意文件或通用 HTTP 代理权限。
- 本地引擎握手通过匿名管道传递，仅接受 `127.0.0.1` 动态端口。能力凭证保留在 Rust 内存，不写入页面 URL、sessionStorage 或原生日志。
- 请求按方法、路由、查询键和大小校验，保留原后端登录与提交授权检查；不跟随重定向、不使用环境代理、不自动重试提交。
- Rust 复用 HTTP 客户端；引擎成功响应支持 HTTP/1.1 连接复用，拒绝请求关闭连接，避免残留请求体污染下一次操作。
- 原生日志位于应用数据目录 `diagnostics/native-events.jsonl`，只记录时间与固定错误代码，不含网址、正文、凭证或原始异常。该日志目前与业务日志分别保存。
- 资料上传使用二进制 IPC，避免把大附件转换成 base64；导出保留原文件内容与原后端的聊天/文件权限边界。

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
| 应用请求协调、站点适配、材料和后台任务 | Python 临时兼容层 | 逐模块迁移并验证，最终移除 Python |
| 应用配置与聊天安全存储 | Rust 调用原 Windows DPAPI | macOS Keychain、移动端安全存储等独立实现 |
| 站点会话与信任设备 | Rust 校验、DPAPI、原子写和同文件跨进程锁 | 登录页面与验证码流程继续迁移 |
| 限速与公共站点 | Rust SQLite 策略、教务处/评课社区 HTTPS 与解析 | 其他网站逐站替换 |
| Chrome 生命周期 | Rust 管理自有进程；Windows Job Object 已验收 | DOM 操作暂由 Python Playwright/CDP 执行 |
| Windows | 本轮构建与验收对象 | 安装器与签名按发布需求处理 |
| macOS/Linux | 尚未支持完整业务引擎 | 移除 DPAPI/msvcrt 等 Windows 依赖，再做原平台验证 |
| Android/iOS | 尚未提供可用安装包 | 确定移动业务执行位置、身份绑定与验证方式 |

Tauri 的跨平台能力不等于现有 Playwright 桌面自动化可直接在手机运行。手机端不能简单打包 Windows 引擎；本轮不默认上传凭证或建立云端代理。iOS 编译和签名还需要 macOS/Xcode 环境。
