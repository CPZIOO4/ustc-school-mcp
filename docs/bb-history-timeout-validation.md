# BB 作业历史页等待修复（2026-10-23）

## 定位结果

同一用户的 BB 门户在 1.672 秒返回 HTTP 200，仍显示已登录账号。历史页的 TCP/TLS 握手约 0.2 秒完成，等待主要发生在首个响应头之前。直连 HTTP 的 Lab1 在 84.047 秒返回真实提交历史；Lab2 在 82.969 秒返回。另两次 Lab1 查询超过诊断用的 100 秒上限。复用包含作业模块 JSESSIONID 的同一个 HTTP 客户端仍然出现慢响应，因此现有证据不支持把它归因于缺少首次模块会话。

Chrome 中直接打开原网站首页成功；未登录的 Chrome 打开同一作业 URL 时显示 Blackboard 的错误页，并提供“登录”入口。此结果单独记录，不能作为已登录访问的结论。

源码修复后的实际 Chrome 读取：目录 6.422 秒完成，后台浏览器 8.922 秒准备完成，9.188 秒发出唯一历史页 GET，131.391 秒收到 HTTP 200，132.079 秒完成解析。返回 `submission_history`、`historical_attempt_present`。其间禁止页面脚本及其他资源请求，没有提交、上传或创建作答尝试。

结论：旧读取器固定等待 25 秒，会在 BB 当前慢响应期间提前失败。校方处理历史页缓慢的内部原因无法从客户端进一步确定；本轮修复客户端提前超时及等待反馈，不将其表述为校方接口提速。

## 实现

- 历史页单次读取总等待上限为 180 秒；只发送一次 GET。Playwright 短等待结束后保持原导航，通过事件等待同一响应，不重放请求。
- 等待期间每秒更新状态，使桌面操作的停止信号和账号变化检查可以生效。停止或最终超时后仍保留原任务及之前的核实记录。
- 识别 HTTP 200 中的 Blackboard 错误模板。含登录入口时进入登录恢复；已登录错误页作为 `upstream_error`，保留旧证据，不作为成功核实。
- 不调整全站网络超时，不更换账号，不删除会话，不刷新已有效的统一身份凭据。

## 验证

- `test_bb_review_navigation.py`：真实本机 Chrome + 延迟 HTTP 服务验证慢响应成功、等待中取消、到期停止；每项均只有一个目标 GET。
- `test_bb_assignment_review.py`：11 项通过，包括读写请求边界、错误页与登录失效分类、历史记录语义。
- `test_study_reader.py`：5 项通过，失败保留旧记录，认证失败进入既有恢复流程。
- `bootstrap.py verify --service bb` 通过，目标指纹 `75140d6ea23f5915b56aa7fbfc8b14c6211f0c45b22bace695caa4f04895ae4d`。
- 实际客户端调用仍为 `restart_required`，启动指纹 `010da620…`；当前对话重载后的核验待完成，无需重新登录学校。

私人证据位于 `.local/study-workspace-evidence/`：`long-history.json`、`warm-history.json`、`fixed-history.log`、`history-wait-verify.json`。原始页面以 DPAPI 加密保存，不进入发布包。

## 桌面发布包验收

在全新发布目录 `dist/native-stage-03fccb7159fc48d3a4ac4313ca71330f/` 启动真实 Tauri/WebView2，以既有本人配置点击 Lab1 的“核实提交记录”：

- 136.203 秒完成，操作状态 `completed`，`history_kind=submission_history`，核实时间更新，旧 `verification_error` 清除。
- 等待秒数持续更新；备注、清单、个人进度和计划时间均保持原值，页面脚本错误为 0。
- 随后点击“打开原作业 / 提交页面”，原生网页标签在 119.938 秒显示真实历史记录标题，登录态继承成功，无需额外登录。
- 此次仅查询和打开原网页，未上传、提交、删除或创建作答尝试。证据为 `history-wait-native.json`、`history-wait-native.png`。

发布 ZIP 共 507 项，CRC、三份 EXE 摘要一致，未含私人目录、DPAPI 或 SQLite 数据。ZIP SHA-256：`5efff935725aaae222bf568a56115761cb74fbdc7f942be060f2e71a642f4a28`。桌面两个既有 SchoolAgent 快捷方式已指向此版本，原参数和用户配置保留。当前对话的 MCP 重载核验仍待完成。
