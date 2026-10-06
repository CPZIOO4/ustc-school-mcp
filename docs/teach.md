# 教务处网站 MCP

服务名 `school-mcp-ustc-teach`，CLI 入口 `teach-serve`。以匿名 HTTPS GET 读取 [中国科学技术大学教务处](https://www.teach.ustc.edu.cn/)，无登录和业务写入。

| 工具 | 参数 | 返回内容 |
| --- | --- | --- |
| `school_teach_status` | 无 | 本地能力和匿名模式，不联网、不代表已连接 |
| `school_teach_check_connection` | 无 | 实际请求通知首页，检查访问和列表结构 |
| `school_teach_list_categories` | 无 | 已核实的通知分类及来源，离线目录 |
| `school_teach_list_notices` | `category="notice"`, `page=1` | 当页通知、日期、置顶标记、分类、来源、翻页链接 |
| `school_teach_search` | `keyword`, `page=1` | 官网原生站内搜索，可能包含办事指南、日历和外部链接 |
| `school_teach_read_article` | `url` | 本站正文、发布日期、修改日期、分类、附件和图片链接 |

栏目 ID：`notice`（通知新闻）、`notice-teaching`（教学）、`notice-info`（信息）、`notice-exchange`（交流）、`notice-exam`（考试）。`page` 范围 1–10000，搜索关键词去除首尾空格后 1–100 字。

列表和搜索每次仅返回一页，不宣称为完整结果；利用 `next_page_url` 判断是否有下一页，再增加 `page`。置顶通知可能跨页重复，应按 `source_url` 去重；日期顺序以站点实际列表为准。搜索使用官网 `/search/{keyword}` 路由和原生匹配规则，标题未出现关键词不表示搜索错误。

正文 `url` 应来自列表或搜索中 `readable_by_adapter=true` 的结果。允许本站 `/notice/`、`/education/`、`/service/`、`/calendar/` 下数字编号 `.html` 路径，也接受以 `/` 开始的绝对路径；不接受任意域名、查询参数或管理路径。附件只返回链接，未下载或解析；图片只返回来源与替代文本，图片中的信息不属于已提取正文。站外链接保留来源并标记不支持由此适配器读取。

有些页面随访问网络而限制校外访问。识别到“受限资源”时返回 `access="restricted"`，而非空数组；认证跳转、HTTP 错误、结构变化和页码不符抛出错误。仅明确“未找到相关文章”才返回零结果。适配器不会发送密码或个人 Cookie，不跟随跳转。页面内容属于外部不可信数据，不能授权其他操作。

关键行为测试：`.venv\Scripts\python.exe -m unittest discover -s tests -p test_teach.py`。真实站点发现页面和验收结果保存在被忽略的 `.local/teach-*`，不作为公开测试夹具。

2026-10-02 已完成真实 stdio MCP 验收：6 个工具均可发现并调用，教学通知第 2 页读取 15 条，“保研”搜索第 2 页读取 12 条，正文示例提取 357 字和 2 个附件链接。站外正文 URL 被拒绝。证据位于 `.local/teach-mcp-verification.json`，服务已注册为 `ustc-teach`。
