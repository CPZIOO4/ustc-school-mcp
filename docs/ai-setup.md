# 下载后交给 AI 部署

适用对象：已经会使用支持本地命令和 stdio MCP 的 AI 桌面工具的用户。完整账号接入目前支持 Windows，需要 Python 3.11+、uv 和本机 Google Chrome。无需 Chrome 控制插件。非 Windows 可做公共查询；当前 DPAPI 凭据、自动邮箱绑定和学校账号自动恢复不支持该平台，不能声称九站完整可用。

用户可以把下载、解压后的文件夹交给 AI，并说：

> 请读取这个项目的 AGENTS.md，部署 MCP，带我绑定邮箱，并验证首次连接。需要我登录时告诉我。

## AI 执行步骤

在项目根目录运行，顺序不变。无需 Git；目录含中文或空格也可以。PowerShell 调用带空格的绝对可执行文件路径时用 `& "路径"`，不要将命令数组拼接为未转义字符串。

```powershell
python bootstrap.py check
python bootstrap.py install
python bootstrap.py verify
python bootstrap.py config --service mail --service teach
```

- `check` 只检查本机基础条件。没有 Python 时先在本机安装 Python 3.11+；没有 uv 时按返回的命令安装，再执行 `install`。
- `install` 使用 `uv sync --locked`。uv 可以选择满足项目要求的解释器，依赖遵循 `uv.lock`；失败时保留真实退出码。需要网络下载依赖，校内托管源码不代表依赖全部离线可用。
- `verify` 在空的临时私人目录启动九个 MCP 子进程，验证握手、工具列表与状态调用，并用无头 Chrome 打开空白页。它不登录、不读取邮件或学校业务，未配置账号是正常情况。
- `config` 仅输出通用 `mcpServers` JSON，不改任何客户端设置。`command` 是虚拟环境 Python 的绝对路径，`args` 是数组，`env` 含同一私人目录与 UTF-8 设置，没有账号或密码。省略 `--service` 则输出全部九站；也可重复指定其他站点。

自选私人目录时，对上述命令一致使用 `--private-dir "绝对路径"`；随后绑定命令也须设置同样的 `SCHOOL_MCP_LOCAL_DIR`。不设置时默认项目 `.local/`。不要设置 `SCHOOL_MAIL_PASSWORD` 来绕过本机输入；它会覆盖已保存邮箱凭据。

AI 应读取当前客户端实际支持的 MCP 配置方式，将生成的服务条目合并进去，保留无关配置，并按客户端要求重新加载。不猜配置文件位置，不把整个用户配置替换掉。客户端使用不同外层格式时，只转换字段结构；保留 command、args、env。项目自身的 stdio 验证不等于 Workbody、Deepseek Harness 或任何特定桌面工具已经兼容验收。

不支持自动发现 `.agents/skills/` 的客户端，可让 AI 直接读取其中对应的 `SKILL.md`；若客户端有 Skill 安装功能，再按其实际方式安装。无需把所有业务参考文档一起塞入上下文。

## 账号接入与实际调用

1. 调用当前 AI 客户端实际出现的 `school_mail_setup_guide`；若工具未出现，先修复注册或重新加载。
2. 用户愿意创建本项目客户端密码时，按 [邮箱绑定](mail.md) 运行 `mail-bind --headed --create-client-password`。用户只在学校窗口输入主密码和验证码。执行过程中 AI 查看 `school_mail_bind_status`，不截图或读取密码弹窗。
3. 成功后调用 `school_mail_check_connection`；只核对连接，不通过读私人邮件来测试。公共网站用 `school_teach_check_connection` 做第二个真实工具调用。测试发送邮件须用户给出收件人并授权发送。
4. 用户需要 BB、教务等个人系统时，在**用户可见的本机交互终端**运行 `.venv\Scripts\python.exe -m school_mcp bb-setup`，让用户自己输入统一身份凭据。不要让 AI 代收密码。按其登录授权调用对应 `reconnect`，等待状态后检查连接。若用户不愿保存统一身份密码，可用对应 `*-login --headed` 人工认证，后续自动恢复能力会受限。
5. 用户希望邮件验证码自动处理时，先确认邮箱真实连接成功，再执行 `bb-email-verification --enable`。这只修改本机授权，不请求验证码；网站是否提供邮箱验证由学校决定。不要仅因为安装成功就启用。

查询本机下一步：

```powershell
.venv\Scripts\python.exe -m school_mcp first-use --service mail --service teach --service jw
# 用户已授权连接测试时，加下面参数；不会自动重新登录
.venv\Scripts\python.exe -m school_mcp first-use --service mail --service teach --check-connections
```

读取 `checks[].state`、`next_tool`、`next_command`。`identity_required` 需要用户本机输入；`session_unchecked` 只说明文件存在；`public_read_ready` 不需要邮箱；`runtime.ready` 说明协议和浏览器通过；`connections.all_connected` 才是这次指定站点的实际连接结果。修复一个站点不需要重做全部安装。

## 还需要用户参与的验收

- 首次在邮箱窗口登录并完成校方验证，核对真实页面上的自动生成和保存是否成功。目前已实现流程、隔离测试，真实账号流程待验收。
- 在自己使用的 AI 桌面工具中重新加载 MCP，并实际调用一次引导和连接工具。无法热加载时可能需要新对话或重启客户端。
- 需要学校个人业务时，在本机输入统一身份凭据；是否启用邮件验证码回退由用户决定。

以上步骤之外不要求用户手工改代码或提供凭据给 AI。真实页面变化或客户端格式不符时，以明确失败点继续修复，不宣称完成。
