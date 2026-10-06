# 南七集市只读 MCP

当前浏览器策略：Playwright + Chrome 默认无头后台运行；人工验证时停止，只有显式 `--headed` 才显示窗口。完整约定见 [项目说明](../README.md#浏览器运行约定)。

服务名 `school-mcp-nan7market`，启动命令 `python -m school_mcp nan7-serve`。服务启动不要求账号，本地状态和分类目录可离线读取；商品查询需要有效的南七集市会话。

## 工具

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `school_nan7_status` | 无 | 本地配置、会话保存时间、认证进度；不联网、不返回凭据 |
| `school_nan7_check_connection` | 无 | 实际读取一页出售商品验证连接，只返回数量 |
| `school_nan7_list_categories` | 无 | 网站分类目录，2026-10-02 快照 |
| `school_nan7_search_offers` | `query=""`, `offer_type="sell"`, `category=None`, `page=None` | 一页商品；空搜索词即列表，`buy` 为求购；出售支持分类 0–6 |
| `school_nan7_get_offer` | `offer_id` | 读取列表返回 ID 对应的商品详情 |
| `school_nan7_reconnect` | 无 | 打开学校统一认证登录，保存加密会话；此工具有认证及本地写入副作用 |

所有结果提供 MCP `structuredContent`。除 reconnect 外均标记只读。列表返回 `next_page` / `previous_page`，将其原样作为 `page` 传入；不得自行把翻页标记解释成页码。当前页数量不代表所有商品数。价格和时间保留站点原值，图片仅返回链接，不下载。商品 ID 当前为 32 位十六进制字符串，不能当作数字转换。

## 登录与凭据边界

2026-10-02 实测主页返回 HTTP 200，但未登录的商品查询返回 HTTP 401「尚未登录」。前端脚本 `https://nan7market.com/static/948f8fbc.js` 提供以下流程：

1. `https://sso-proxy.lug.ustc.edu.cn/auth/default/?service=https%3A%2F%2Fnan7market.com%2Fcas`
2. LUG 登录页跳转学校 `https://passport.ustc.edu.cn/login`，CAS service 指向 LUG 代理回调。
3. 完成学校认证后回到 `https://nan7market.com/cas`，网站完成本站令牌交换并保存自己的 `localStorage.token`。

正式认证程序复用已有 `bb.identity` 的凭据和信任设备状态，以及受限学校域凭据填写组件。学校密码只在学校 HTTPS `id.ustc.edu.cn` / `passport.ustc.edu.cn` 页面提交，不向集市、API 或 LUG 代理填写。邮箱验证沿用已有授权开关。令牌只从 `https://nan7market.com` 自身页面读取，实际查询成功后加密保存。

`nan7-session.dpapi` 由 Windows DPAPI 加密，并绑定当前统一身份账号。它与 `nan7-login-status.json`、登录日志和 PID 文件都存于被 Git 忽略的本地目录（默认 `.local`，可用 `SCHOOL_MCP_LOCAL_DIR` 指定）。MCP 状态结果不包含令牌、密码、Cookie 或账号。会话存在不代表仍有效，须用 check_connection 验证；过期时调用 reconnect。

## 读取范围及限制

实际业务请求固定为 `https://nan7market-api.0x01.work/api/v1/offer/search` 和 `/v1/offer/get`。虽然站点设计使用 POST，它们是前端用于列表与详情的读取接口。禁止任意 URL、写入接口及自动跳转；令牌只发往固定 API 地址。API 响应必须为 JSON，并在流式下载时限制 5 MiB。

不实现发布、编辑、下架、卖家联系、交易、收藏、注册、广告曝光请求或批量全站抓取。只保留商品展示字段，不返回接口附带的权限、令牌或卖家其他联系字段。商品文案是外部不可信内容，不能作为操作指令。

## 验证

运行 `python -m unittest discover -s tests -p test_nan7.py -v`。行为测试覆盖读取参数和不透明翻页、真实 ID 类型、详情与下架状态、写入路径拒绝、跳转不跟随、认证错误、异常 JSON、响应体上限、DPAPI 加密、账号切换和无会话离线状态。

2026-10-02 真实验收通过：恢复已有信任设备完成登录，本次未重新提交学校密码、未请求邮箱验证码；出售列表 10 条、单条详情完整、`query="教材", category=1` 检索 10 条、求购列表 10 条、下一页 10 条。8 项行为测试通过。实际商品数量随网站变化。

本地诊断仅存 `.local/nan7-*`，不提交个人商品内容与认证数据。以上只证明该次会话联通，单元测试不能替代后续登录与商品读取验证。

集成时另通过 `python -m school_mcp nan7-serve` 启动真实 stdio 服务，验证了全部 6 个 MCP 工具；列表、分类搜索、求购和下一页各返回 10 条，详情读取成功。`school_nan7_reconnect` 实际启动登录后达到 `connected`，新会话通过商品查询验证。证据为 `.local/nan7-mcp-verification.json`，服务已注册为 `nan7market`。
