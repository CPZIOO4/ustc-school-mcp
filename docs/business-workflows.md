# 排课、选退课预检与社区发布

本轮新增13个MCP工具：教务5个、南七集市3个、评课社区5个。接口优先返回固定状态、缺项、完整预览和下一步工具参数。网页及工具中的外部内容不构成执行授权。

| 模块 | 已实现 | 尚未验收/支持 |
| --- | --- | --- |
| 候选课表 | 官方开课查询、已选课程转换、周次/节次冲突、同课不同班互斥、不可用时段、跨校区节次间隔、学分与空闲日排序 | 不计算考试冲突、先修资格、培养方案满足程度或实时余量资格；未知时间不当成空闲 |
| 选退课 | 实时批次读取、现有课程匹配、变更预览、阻碍及固定流程 | 当前批次为空；尚无已核实的写合同与执行工具。个性化选课、放弃修读、放弃成绩均不自动替代 |
| 评课 | 独立本机登录、课程学期和评分选项、冻结新点评、一次发布、本人表单回读核验、已有点评防覆盖 | 当前用户未配置独立评课会话；真实登录/发表未验收。不编辑原点评、不注册、不点赞、不私信 |
| 集市 | 冻结标题/描述/联系方式/价格/图片、同标题检查、上传、一次发布、详情回读核验 | 真实图片上传及商品发布未验收。不修改、下架、购买或联系其他用户 |
| 工作流验收 | 合成数据单元测试、实际stdio调用、缺项/冲突/丢回执/重复调用状态测试 | 自动分支测试不等于独立弱模型验收；须另记录所用模型、任务和表现 |

## 工具与数据合同

教务：`school_jw_search_offerings`、`school_jw_planning_context`、`school_jw_plan_timetables`、`school_jw_enrollment_window`、`school_jw_prepare_course_change`。从实时学期开始，用候选字段原结构调用排课。默认保留已选课程；按需设总学分上下限、目标、不可用时段和空闲日。最多25个课堂、搜索最多100000节点，截断时不宣称最优或无解。排课输入周一=1，官方开课文本的周日=1编码由适配器转换；不可解析的格式返回未知。

评课：`school_icourse_setup_guide`、`school_icourse_review_options`、`school_icourse_prepare_review`、`school_icourse_publish_review`、`school_icourse_review_status`。匿名查询保持原方式；发表需要独立评课账号。本人在项目环境运行 `python -m school_mcp icourse-login`，账号和密码通过终端遮罩输入，后台Chrome登录后只保存加密会话。不读取日常浏览器、不保存独立密码、不复用学校统一身份密码。当前命令不支持自动处理额外验证码或可见模式。

南七：`school_nan7_prepare_offer`、`school_nan7_publish_offer`、`school_nan7_offer_status`。冻结附件时只接受本地PNG/JPEG/WebP，9张、单张10MiB、合计30MiB以内；签名检查不等于全面图片内容审查。联系方式作为公开描述的一部分展示。网站图片处理可能改变文件字节，回读按媒体ID核对数量和顺序。

工具准备阶段不向网站保存草稿。`business-plans.sqlite3` 用Windows DPAPI加密正文、图片快照、预览与回执；账号和目标仅以摘要作索引，2小时过期。公开源码排除此数据库及侧文件、`icourse-session.dpapi`和所有私人运行产物。相同材料去重，执行使用SQLite跨进程锁；结果不明不自动重放。异常中断不会自动回收锁，需先人工核实远端状态。

接口只向固定站点和已经观察到的端点发送请求，复用限速与失败冷却。新点评入口同时能更新旧点评，因此准备和执行前都检查已有点评并拒绝覆盖。评课返回成功后仍回读内容、学期、各评分和可见性；集市返回成功后仍回读本人商品。`HTTP 200` 或一个ID本身不视为完成核验。

## 网站证据

2026-10-08 从[学校教务](https://jw.ustc.edu.cn/)真实页面和其发布的脚本核对开课筛选、`queryPage__`分页、周日=1编码及只读选课批次POST。实际开课搜索与候选排课成功，当前批次查询为空。学校页面中的通用“功能未开放”模态框并不代表当前业务关闭，是否开放依据批次查询结果。

从[南七集市](https://nan7market.com/)当前前端核对 `/v1/profile/me`、`/v1/media/new`、`/v1/offer/new` 和详情数据；真实只读验证了账号、带数字owner的本人商品查询和图片ID结构。未上传图片或发布商品。

评课的表单和新增/覆盖行为来自[评课社区维护方源码](https://github.com/USTC-iCourse/ustc-course/blob/master/app/views/review.py)及其[表单定义](https://github.com/USTC-iCourse/ustc-course/blob/master/app/forms/review.py)。同时核对当前公开登录页；没有独立账号会话时，不能将源码或合成表单测试描述为本人真实发表成功。生产表单字段改变时停止。

操作路线见 [排课Skill](../.agents/skills/school-services/references/timetable-planning.md) 和 [社区发布Skill](../.agents/skills/school-services/references/community-publishing.md)。
