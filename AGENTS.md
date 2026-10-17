# 给部署本项目的 AI

用户已在使用能执行本地命令、支持 stdio MCP 的 AI 工具。本文件是部署入口；不要求用户理解 Python、Git 或手写配置。不假定某个客户端的配置位置或格式，也不把 Workbody / Deepseek Harness 等名称当作已经完成兼容验收的证据。

1. 读取 [AI 部署流程](docs/ai-setup.md)，在这个文件所在目录执行。下载 ZIP 后无需初始化 Git。
2. 用 `python bootstrap.py check` 检查，再用 `python bootstrap.py install` 安装锁定依赖。命令失败先解决输出中的缺项。
3. 用 `python bootstrap.py verify` 验证全部九个 stdio 服务和后台 Chrome。此检查不访问学校、不读取邮件内容或学校业务数据、不发邮件。
4. 用 `python bootstrap.py config --service mail --service teach` 生成首批客户端配置。合并到用户当前 AI 工具的 MCP 设置，保留其他服务；只注册用户需要的模块。加载项目的 `.agents/skills/school-onboarding/SKILL.md`；业务操作另读 `school-services`。
5. 调用实际加载的 `school_mail_setup_guide`。用户选择绑定并创建一个客户端密码后，运行它返回的浏览器绑定命令，让用户在窗口中登录。密码不经过聊天、命令参数或 AI 的 stdin。
6. 用 `school_mail_bind_status` / `school_mail_check_connection` 核对。不要将配置生成、进程启动或历史状态当成接入成功。只有明确发送测试邮件的请求才发信。

更新已部署项目时，按 [更新核验流程](docs/runtime-updates.md) 执行。`verify` 只验证新建进程，之后须在实际客户端调用对应 `school_<站点>_runtime_status` 并传入自检目标指纹。服务重载保留私人目录与登录状态，不批量结束其他 Python 进程。

默认浏览器为本机 Chrome，无头。原生客户端的 BB 提交、青春科大固定流程、官方人工登录和邮箱绑定浏览器流程使用 Rust CDP；非原生入口及尚未迁移流程使用 Playwright。两者均不依赖浏览器控制插件，不向模型开放任意 JavaScript、CDP 命令或网址。首次邮箱绑定的 `--headed` 是用户参与登录的专用入口。已有可用邮箱不应为了演示而再生成密码。学校统一身份凭据与邮箱专用密码不同，按需要在本机录入，邮件验证码回退按用户授权单独启用。

本轮使用的私人目录必须贯穿安装、客户端配置、绑定与检查；默认项目 `.local/`，支持 `SCHOOL_MCP_LOCAL_DIR`。不上传或复制他人的 `.local/`；不能从聊天历史提取账户填进新用户配置。Git 只包含源码与通用资源；Windows 二进制只从 `packaging/build.ps1` 的资源白名单构建专用包，不能压缩工作目录或用户数据。不要删除绑定记录来绕过“结果未知”或重复生成客户端密码。

Windows 本机应用的独立入口见 [应用说明](docs/desktop.md)。它使用 `%LOCALAPPDATA%/USTCSchoolAgent`，不要自动导入原 MCP 私人目录。本人在官方页面登录后只保存会话，不保存学校密码；需重新验证时使用应用账号页。普通网页操作仍默认无头 Chrome。

原生桌面宿主位于 `native/`，Rust 核心位于 `native/core/`，构建见 [原生客户端说明](docs/native-desktop.md)。目标是全面迁移到 Rust，不做速度对比。已迁移模块必须接入实际执行路径；Python 仅为尚未迁移模块的临时兼容层，不能把包装 Python 调用称为完成 Rust 迁移。当前范围见 [迁移进度](docs/rust-migration.md)，未完成全量 Rust 或手机端迁移。发布使用 `packaging/build-native.ps1` 的白名单引擎与全新暂存目录；不要把真实数据目录或开发工具链放入包。验收原生版须使用真实 Tauri/WebView2 和 Rust IPC，普通 Chrome 网页测试不能代替原生窗口验收。

0.7.0 的模型配置/能力测试、候选配置切换、应用文件上传校验与对话中断恢复，以及 BB 附件传输/一次性提交锁和青春科大 DOM/监控/系统调度已接入 Rust。0.8.0 原生主后端、对话协调及官方人工登录由 Rust 执行。Python 普通 HTTP/MCP 入口、邮箱绑定/验证码及其余业务仍存在；原生版仅通过固定私有管道调用临时适配器，不删除引擎依赖。合成 Chrome 提交测试不等于真实学校提交验收，也不提供速度提升或零缺陷保证。

0.9.0 源码继续迁移邮箱绑定浏览器及验证码筛选、业务日志和应用内订阅的状态/存储/简报。IMAP/SMTP、完整验证码回退、订阅采集和后台 worker 仍有 Python。原生诊断不能因通信失败退回旧落盘，日志失败不改变业务回执；订阅从邮件切为应用内不能抹掉旧未知投递记录。源码、开发核心测试与 MSVC/Tauri 发布包分别验收；见验证记录，不能把旧安装包当作新版交付。

青春科大新原生计划通过固定 `school-core --young-worker` 入口运行，执行前核对程序摘要、版本和当前账号。0.6 旧原生计划须在当前应用重新复核，不能交给旧 Python worker 继续执行；原记录与未知结果保留。需要重新登录时停止计划，不在原生 worker 内回退 Python 登录或重新发送报名请求。
