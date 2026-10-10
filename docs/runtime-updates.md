# 更新与运行版本核验

九个服务各有一个 `school_<站点>_runtime_status` 工具；站点为 `mail`、`bb`、`jw`、`library`、`teach`、`nan7`、`icourse`、`young`、`finance`。它离线比较进程启动时的源码及依赖元数据与当前磁盘，不读取账号、Cookie、任务数据库或学校业务。

## 固定更新流程

1. 更新前，调用需要使用的服务的 runtime_status，保留 `instance_id`。初次升级到带此工具的版本时，旧进程没有该工具是正常情况，需要先完成下列重载。
2. 按用户要求更新源码，保留 `.local/` 或用户指定的私人目录，以及未提交的本地改动。不要用覆盖整个目录的方式更新。不自动拉取远端或执行 Git 操作。
3. 项目依赖文件发生变化时运行 `python bootstrap.py install`，然后运行 `python bootstrap.py verify`；可以用 `--service finance` 等参数限定本次需要的服务。依赖安装遵循锁文件；版本工具不代替锁文件安装验证。
4. `runtime.ready=true` 后，保留 `runtime.client_verification.expected_fingerprint`。按用户所用客户端实际支持的方式，重载相应 MCP 服务；无需重新登录学校。若客户端没有单服务重载，则按客户端指引刷新连接或重启客户端，不假定新对话必然启动新进程，不按进程名批量结束 Python。
5. **在当前 AI 客户端中**调用各服务的 runtime_status，传入该 `expected_fingerprint`。只有 `state=current` 且 `expected_matches=true` 才完成版本核验。已有旧 `instance_id` 且确实执行了重载时，新值应不同。结果由实际工具返回，不让模型自行填成功。
6. 需要使用学校账号时，再按已有授权调用相应的连接检查。版本匹配与账号连通是两个独立结果；不为版本更新清除会话或重新生成邮箱密码。

## 返回状态与处理

| state | 含义 | 下一步 |
| --- | --- | --- |
| `current` | 启动时指纹与本机文件一致 | 更新验收还须传入目标指纹并确认 `expected_matches=true` |
| `restart_required` | 项目源码或依赖元数据在启动后变更 | `dependency_sync_required=true` 时先 install，再 verify、重载及客户端核验 |
| `expected_mismatch` | 当前进程与所在目录一致，但不匹配本次自检目标 | 检查客户端是否指向另一份项目或环境，修正对应配置再重载 |
| `check_failed` | 源文件不可读、缺失、存在不受支持的链接或超出扫描限制 | 检查安装完整性；不能解释为当前版本或无限循环重载 |
| 工具不存在 | 可能仍在使用不含此工具的旧服务或旧工具列表 | 重载服务并刷新工具列表，再核验；不能拿另开的 CLI 进程冒充当前客户端 |

`actions` 按执行顺序返回上述动作代码：`sync_dependencies`、`verify_local`、`reload_client`、`verify_client`、`check_client_configuration`、`check_source_files`。不需要模型从错误文本猜测。

## 证据范围

- `startup_fingerprint` 是启动时包内 Python 源码、项目依赖文件（源码安装时）和关键已安装包版本的组合 SHA-256；`disk_fingerprint` 是当前值。另返回源码专用指纹、启动时间和随机进程实例标识。它不是 Git 提交号，同一提交在不同依赖环境中可能不同。
- 不依赖 `.git`，适用于 ZIP 和源码目录；也支持已安装包，但已安装包不向上扫描项目依赖文件。修改说明文档、日志、私人目录或字节码缓存不会误报代码更新。
- 这是启动时源文件的证据，不是 Python 内存转储，也不证明所有模块已加载、客户端支持自动重载、依赖完全符合锁文件或远端没有更新。不提供进程自杀、批量结束进程或重放业务操作的工具。
- 从这一版本开始启动的进程才会记录基线。旧版常驻进程必须先重载一次，才能提供后续更新检测。
