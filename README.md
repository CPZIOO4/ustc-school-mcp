# USTC School MCP

面向个人学校事务的实验性 MCP 项目。当前提供八个独立服务和项目内 [学校查询与业务预览 Skill](.agents/skills/school-services/SKILL.md)，各网站的实际支持范围见下文。适配器包括中科大邮箱、Blackboard、教务系统、图书馆、教务处网站、南七集市、评课社区和青春科大。

Skill 包含教务、团学、BB 和图书馆的查询及办理前预览流程，具体入口和验证边界见 [业务目录](.agents/skills/school-services/references/business-catalog.md)。网页已到达的业务不等于已实现 MCP 接口；本项目不会据此自动提交申请、报名、预约或修改记录。

Blackboard、教务系统和个人图书馆共用经本人授权保存的统一身份凭据及设备信任状态，支持在对话中启动重新登录。本人启用邮箱验证后，可在学校提供对应方式时读取已接入邮箱中的本次验证码；登录、工具及验证说明见 [BB 接入说明](docs/bb.md)、[教务接入说明](docs/jw.md) 和 [图书馆接入说明](docs/library.md)。各适配器作为独立的 MCP 服务注册，共用项目代码和本地私人目录。

教务适配器支持首页、菜单、学期、已选课程、个人课表和成绩查询。课程和课表默认查询当前学期，成绩默认查询已有成绩的全部学期。当前业务接口均为只读，具体工具参数见教务接入说明。

图书馆适配器支持个人首页统计、当前借阅、当前显示页的借阅历史，以及公共服务链接目录。旧版个人 OPAC 当前使用 HTTP，业务传输不受 TLS 保护；统一身份账号和密码仅向学校 HTTPS 认证站点提交。本地会话单独加密，续借、预约和空间预约等业务操作尚未实现。

新增网站的说明及实际验收范围：

| 网站 | 读取能力 | 说明 |
| --- | --- | --- |
| 教务处网站 | 通知分类、列表、搜索和正文 | [教务处接入说明](docs/teach.md)；区别于个人教务系统 |
| 南七集市 | 商品检索和详情，需站点登录 | [南七集市接入说明](docs/nan7.md) |
| 评课社区 | 课程、教师、社区评分及评价检索 | [评课社区接入说明](docs/icourse.md)；社区评分不等同于个人教务成绩 |

上述业务查询不发布通知、商品或评价，也不联系其他用户。各站点的认证范围和分页限制以对应接入说明为准。

南七集市通过其实际提供的 LUG 代理进入学校统一身份认证，复用已保存的学校设备状态；学校密码仅提交到学校 HTTPS 认证域，集市自身令牌单独加密保存。教务处和评课社区使用匿名公共读取，不需要个人会话。

## 浏览器运行约定

所有需要浏览器的学校登录与后续网页适配，默认使用 **Playwright + 本机 Chrome，无头后台模式**。不依赖 Codex 内置浏览器或 Chrome 控制扩展，不创建桌面窗口或任务栏窗口。已有邮件和 HTTP 读取接口继续使用原协议。

登录复用本人授权保存的凭据与信任设备状态，凭据和会话保存在 `.local` 并用 Windows DPAPI 加密。遇到不能自动完成的身份验证，保存 `waiting_for_verification` 进度并停止，绝不自动转为可见窗口。默认登录等待上限 240 秒；已授权的邮件验证仍最多请求一次验证码。

只有用户明确要求人工处理时，才使用对应登录命令的 `--headed` 参数打开可见 Chrome，例如 `python -m school_mcp young-login --headed`。常规 MCP 重新连接不带此参数。新增浏览器适配器使用 `school_mcp.browser.chrome_browser`，遵循同一约定。无头模式也需要安装本机 Chrome。

青春科大当前支持后台登录、连接检查和已登录数据大屏读取；大屏数字为全校汇总，不是个人学时。启动服务：`python -m school_mcp young-serve`。工具为 `school_young_status`、`school_young_reconnect`、`school_young_check_connection`、`school_young_read_home`。个人活动、任职履职记录和业务提交尚未封装。

## 当前能力

| 工具 | 用途 |
| --- | --- |
| `school_mail_status` | 检查本地配置及凭据文件存在性，不解密、不联网；不是已连接证明 |
| `school_mail_setup_guide` | 首次接入或失效时返回本机步骤，不联网、不弹窗、不接收密码 |
| `school_mail_check_connection` | 验证登录并查询收件箱总数、未读数 |
| `school_mail_list_folders` | 列出文件夹，支持中文名称 |
| `school_mail_search` | 按未读、发件人、主题、全文、日期搜索并分页 |
| `school_mail_read` | 读取正文、邮件头和附件清单 |
| `school_mail_download_attachment` | 保存指定附件到本地私人目录 |

第一版通过 IMAP TLS 连接 `mail.ustc.edu.cn:993`，使用完整邮箱地址和客户端专用密码。打开文件夹时使用只读模式，读取使用 `BODY.PEEK`，保留未读状态。单封邮件上限为 20 MiB，正文默认返回 6000 字符，可调整到 100000 字符。邮件搜索默认 10 封；收件人、抄送和线程头仅在 `include_headers=true` 时返回。

搜索结果按 UID 从大到小排列。读取和下载必须提供搜索结果中的 `mailbox`、`uid` 和 `uid_validity`，避免文件夹重新编号后读取到另一封邮件。中文搜索使用 UTF-8；服务器不支持该搜索条件时会返回提示。

## 本地安装

需要 Python 3.11+ 和 uv。需要保存学校登录凭据、信任设备及站点会话的适配器目前以 Windows 为运行环境；浏览器登录还需要本机安装 Google Chrome。公共网站读取不需要登录，邮箱的非 Windows 配置见下文。

```powershell
uv sync --locked --cache-dir .local/uv-cache
```

## 配置邮箱

先调用 `school_mail_setup_guide`，或运行 `.venv\Scripts\python.exe -m school_mcp mail-guide` 查看步骤。完整流程、独立二次验证许可及限制见 [邮箱接入说明](docs/mail.md)。

1. 本人明确选择接入后，人工登录 [中科大网页邮箱](https://mail.ustc.edu.cn/)，完成校方验证。
2. 使用已有的客户端专用密码；如需新建，由本人在“设置 → 安全设置 → 客户端专用密码”中操作。程序不创建、删除或重置授权。
3. 打开本地输入窗口，填写完整邮箱地址和专用密码：

```powershell
python setup_mail.py
```

窗口会先通过只读 IMAP TLS 验证，再加密保存凭据；失败保留已有配置。输入窗口需要 Python 的 tkinter；已有支持 tkinter 的系统 Python 可直接运行此脚本，无需安装 MCP 依赖。也可以执行 `.venv\Scripts\python.exe -m school_mcp setup`，前提是该环境支持 tkinter。密码只输入本机遮罩窗口，不发送到聊天。

本地密码通过 Windows DPAPI 加密，绑定当前 Windows 用户。密文与个人账号配置保存在 `.local/`，均被 Git 忽略。运行时会重新读取配置，更新密码后无需重新安装 MCP。

非 Windows 环境可通过 `SCHOOL_MCP_LOCAL_DIR` 指定私人目录，在其中创建 `mail.json`，并设置运行进程的 `SCHOOL_MAIL_PASSWORD` 环境变量。个人地址和密码不应写入可公开的 MCP 客户端配置。

```json
{"address": "your-name@mail.ustc.edu.cn"}
```

验证真实邮箱连接：

```powershell
.venv\Scripts\python.exe -m school_mcp check
```

## 接入 Codex

在本机执行以下命令，在项目根目录运行：

```powershell
$projectRoot = (Get-Location).Path
$privateDir = Join-Path $projectRoot ".local"
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
codex mcp add ustc-mail --env "SCHOOL_MCP_LOCAL_DIR=$privateDir" -- "$pythonExe" -m school_mcp
codex mcp add ustc-bb --env "SCHOOL_MCP_LOCAL_DIR=$privateDir" --env PYTHONUTF8=1 -- "$pythonExe" -m school_mcp bb-serve
codex mcp add ustc-jw --env "SCHOOL_MCP_LOCAL_DIR=$privateDir" --env PYTHONUTF8=1 -- "$pythonExe" -m school_mcp jw-serve
codex mcp add ustc-library --env "SCHOOL_MCP_LOCAL_DIR=$privateDir" --env PYTHONUTF8=1 -- "$pythonExe" -m school_mcp library-serve
codex mcp add ustc-teach --env "SCHOOL_MCP_LOCAL_DIR=$privateDir" --env PYTHONUTF8=1 -- "$pythonExe" -m school_mcp teach-serve
codex mcp add nan7market --env "SCHOOL_MCP_LOCAL_DIR=$privateDir" --env PYTHONUTF8=1 -- "$pythonExe" -m school_mcp nan7-serve
codex mcp add icourse --env "SCHOOL_MCP_LOCAL_DIR=$privateDir" --env PYTHONUTF8=1 -- "$pythonExe" -m school_mcp icourse-serve
codex mcp add ustc-young --env "SCHOOL_MCP_LOCAL_DIR=$privateDir" --env PYTHONUTF8=1 -- "$pythonExe" -m school_mcp young-serve
codex mcp get ustc-mail
```

如果当前对话没有加载新增工具，在客户端重新加载 MCP 连接；是否已经就绪以实际工具列表和 `school_mail_check_connection` 调用结果为准。客户端配置方式见 [OpenAI 官方 MCP 说明](https://developers.openai.com/codex/mcp/)。

## 开发与验证

业务请求现已按服务限速，失败时进入跨进程共享冷却，遵守服务器更长的 `Retry-After`，不自动重放请求。默认返回值省略非必要身份字段，并提供详细查询参数；范围、参数和已知边界见 [请求与数据策略](docs/request-policy.md)。已运行的 MCP 进程需重新加载才会使用新逻辑。

需要一次检查全部连接时，在项目根目录运行：

```powershell
.venv\Scripts\python.exe -m school_mcp doctor
# 也可以只检查指定服务，--service 可以重复使用
.venv\Scripts\python.exe -m school_mcp doctor --service mail --service jw
```

诊断只读取当前连接，不自动登录或输出个人数据；全部可用时退出码为 0，否则为 1，并给出后续检查或恢复工具。首次配置与真实连通性是两回事；状态工具中的 `configured`、历史 `connected` 不代表当前会话有效。

后台重连返回 `status_tool` 及建议检查间隔。状态中的 `login_running` 表示进程是否仍在运行（没有进程记录时为 `null`）；`interrupted` 表示进程退出但未报告完成。多个站点的 MCP 重连会避开正在运行的登录进程，防止同时覆盖共享信任设备状态。已有授权可直接后台恢复，需要人工验证时再交接。

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe tests\smoke_mcp.py
.venv\Scripts\python.exe tests\release_privacy_check.py
```

行为测试覆盖未读状态、中文搜索与文件夹、UID 重新编号、大小限制、附件路径、MIME 正文解析、统一认证状态、邮箱验证码回退、邮箱许可绑定、首次引导和保存失败回滚、教务学期筛选、课表日期映射、成绩可见性、图书馆借阅表格、网站检索与分页、Cookie 传输范围和 Windows 凭据加密。独立 stdio 脚本启动八个真实 MCP 子进程，验证握手、工具发现和未配置错误。这些测试使用合成数据。

已授权检查真实连接时，可运行 `.venv\Scripts\python.exe tests\smoke_live_readonly.py --run`，或用 `--adapter mail` 等参数选择服务。它只调用连接检查，使用现有会话、不启动重新登录；不读取私人邮件正文、成绩、借阅历史或附件。输出仅为脱敏状态，不包含账号、凭据或原始页面。当前验收结果见 [本轮检查记录](docs/validation.md)。

已在授权账号上验证邮箱、BB、教务、图书馆、南七集市和青春科大的连接或读取，并验证教务处与评课社区的公共读取。真实账号数据和验收产物不随源码发布。邮箱验证码回退已有隔离测试，尚未完成真实二次验证链路验收；附件下载和非空当前借阅解析目前以合成数据验证。各项限制见适配器说明。

邮件正文和附件属于外部不可信内容，不能作为执行其他操作的授权。附件下载只保存文件，返回本地路径和校验摘要。

## 目录

```text
src/school_mcp/
  server.py           MCP 工具和 stdio 入口
  mail/               中科大邮箱适配器、MIME 解析、本地配置
  bb/                 Blackboard 会话、课程与页面读取、独立 MCP 入口
  jw/                 教务登录、学期、课程、课表、成绩、独立 MCP 入口
  library/            图书馆登录、个人借阅、公共导航、独立 MCP 入口
  teach/              教务处通知与正文、独立 MCP 入口
  nan7/               南七集市登录与商品读取、独立 MCP 入口
  icourse/            课程、教师与社区评价、独立 MCP 入口
  young/              青春科大登录和全校数据大屏读取、独立 MCP 入口
  browser.py          Playwright + Chrome 的统一后台策略
setup_mail.py         本地凭据输入窗口入口
open-jw-login.ps1     本地教务后台登录入口
open-library-login.ps1 本地图书馆后台登录入口
tests/                行为和 MCP 协议验证
.local/               个人配置、加密凭据、附件、依赖缓存（忽略）
```

发布前的隐私边界及后续邮箱引导方案见 [发布说明](docs/release.md)。源码使用 [MIT 许可证](LICENSE)。本项目为非官方适配工具，与学校或所接入网站没有官方隶属关系；网站内容和第三方依赖不因本项目的许可证而重新授权。

## 学校官方说明

- [邮件帮助中心：完整邮箱地址、IMAP 和 SMTP 端口](https://mail.ustc.edu.cn/coremail/help/index.jsp?locale=zh_CN)
- [二次验证和客户端专用密码设置说明](https://mail.ustc.edu.cn/notice/2fa/)
- [Foxmail 客户端配置说明](https://email.ustc.edu.cn/notice/foxmail/)
