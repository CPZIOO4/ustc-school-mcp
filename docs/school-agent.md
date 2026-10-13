# 在现有 AI 客户端中使用学校智能体

学校智能体由宿主 AI、`school-agent` Skill、可选协调服务和各站点 MCP 组成。宿主提供语言理解与推理；协调服务不调用模型 API，也不在聊天结束后自行思考。已封装的后台任务仍由独立脚本执行。

不使用外部 AI 客户端时，可使用 [Windows 本机应用](desktop.md)，由应用连接本人提供的模型 API 并运行同一协调流程。下面的 MCP 部署步骤不适用于 EXE 用户。

## 部署

在项目根目录完成原来的 `bootstrap.py install`，然后：

```powershell
python bootstrap.py verify --service agent
python bootstrap.py config --service agent
```

把生成的 `school-agent` 配置合并到所用 AI 客户端，加载项目内 `.agents/skills/school-agent/SKILL.md`，按客户端支持的方式重新加载 MCP。使用同一 `SCHOOL_MCP_LOCAL_DIR` 即可复用原有账户和任务；不用再把九站全部注册到模型上下文。原有九个独立服务仍可保留，日常从协调入口处理即可。

启动命令是 `python -m school_mcp agent-serve`。默认 `bootstrap.py config/verify` 仍面向原九站，协调服务需显式选择。首次账号绑定仍按 [首次接入](ai-setup.md)；加密任务存储使用 Windows DPAPI，目前智能体任务入口要求 Windows。默认无头 Chrome。

Codex 可按 [官方 MCP 配置说明](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) 使用 `codex mcp add` 登记 stdio 服务。涉及后台浏览器的单次提交可能超过默认调用时限，可为此服务设置 `tool_timeout_sec = 300`；超时后仍必须核实原记录，不能重复提交。

独立 verify 通过后，在实际客户端调用 `school_agent_runtime_status(expected_fingerprint=verify给出的值)`；只有 current 且 expected_matches=true 才确认该进程加载了目标版本。verify 不证明账号已登录。

## 六个业务入口

| 工具 | 用途 |
| --- | --- |
| school_agent_route | 根据目标提供流程提示；不自动执行，歧义由宿主澄清 |
| school_agent_start | 为选定流程保存目标与当前身份绑定 |
| school_agent_tools | 列相关工具；指定名称时获取原工具的参数 schema |
| school_agent_call | 检查范围、参数和已有授权，分派一次原工具调用 |
| school_agent_task | 恢复任务状态；需要时返回历史目标和参数 |
| school_agent_tasks | 分页找回当前身份可用的任务 |

另有运行版本检查工具。支持邮件、BB、个人教务、候选排课、青春科大、财务、图书馆、教务通知、评课、集市、一网通办、学工、学习空间、云盘、Vlab 和公开网页十六种任务流程。查询、准备、执行和恢复细节复用已有 Skill。关键词路由是提示，不是完整语言理解；仍需要宿主 AI。

例如“帮我准备一笔差旅报销”：route → start(finance) → tools(preflight) 获取 schema → call(preflight) → 按 input_guidance 收集材料 → 再次预检。最后说明仅完成材料预检，没有创建报销单。报销办理还需真实任务进一步适配。

## 上下文与执行边界

目标、账号摘要、每次调用参数、时间、授权声明和精简状态回执加密保存在私人目录 `agent-tasks.sqlite3`。查询正文只返回给宿主，不存进任务；公开仓库不包含该数据库。默认任务查询不返回完整参数，显式 include_context 才返回。不要把秘密写进目标或参数；键名检查只能拦住常见凭据字段，不能识别所有自由文本秘密。

授权声明只覆盖确切工具和参数，不是账号的永久授权。程序不能从聊天证明真人是否授权，宿主必须依据用户原话设置 authorized。原工具的账号、快照、截止日期和幂等检查继续有效。

执行前先提交本机调用记录。相同任务内相同执行参数最多尝试一次；失败、结果未知或进程中断时，只查询原回执，不自动解锁。底层业务记录仍是最终依据。此保守机制可能需要人工核实，不承诺任意崩溃后自动续办。客户端正在调用时也不应更换本机账号。

任务最多 100 次调用，每次参数最多 128 KiB，上下文最多 2 MiB；无通用 shell、任意脚本、自动开放工具或不受限浏览器执行能力。网站返回的工具名称不会自动加入允许列表。查询可能保存精简本机记录；返回数据仍遵循原站点的最小化策略。

协调器的 active/returned 不表示业务成功；scheduled 只表示已安排。具体完成与否必须查看原 result 和 links 对应的业务状态。能力没有自动扩大：财务不提交报销，选退课无写接口，青春科大作品提交未适配。

常用流程可直接 start；返回 entry_tool 的真实 schema 后可直接 call，其他工具再用 tools 获取。只有不清楚目标所属流程时才需要 route。恢复时 task 返回的是原任务快照，后续先查其 links；快照的 needs_verification 不是新的调用失败。
