# 教务查询与办理前预览

起点：`school_jw_list_modules`，只用实时返回的入口。以下路径均相对 `https://jw.ustc.edu.cn`。菜单存在只证明当时可见；表格中的入口于 2026-10-05 导航到达，按钮和业务结果没有逐项执行验证。

## 课表、成绩与 GPA（已有 MCP）

1. 用 `school_jw_list_semesters` 确认学期名称和 ID；“大三”先对应入学年和学年，不直接取当前学期。
2. 课表用 `school_jw_timetable`；已选课程用 `school_jw_list_courses`；成绩用 `school_jw_grades`。成绩的 `semester_id=0` 是全学期，不是当前学期。
3. 回答成绩时保留学校提供的统计口径；关键词只筛选课程行，不改变返回汇总。学年跨两学期时，不能直接平均两个学期 GPA。只有学分、绩点、纳入规则齐备时才另算，并标注为估算。
4. “去掉某课后 GPA”只做情景计算，核对重修、放弃成绩与计入规则，不真的退课，也不把“放弃成绩”和“放弃修读”当同一操作。缺少权重或课程绩点时报告缺口。

交付：学期范围、课程/统计值、学校原值或另算、未公开或缺失数据。不要为查询读取学籍全档案。

## 选课与培养方案

| 菜单 | 已观察路径 | 预览目的与停止点 |
| --- | --- | --- |
| 学生选课 | `/for-std/course-select` | 仅进入首页；选课批次和可选课程未验证，不选课 |
| 个性化选课与放弃修读 | `/for-std/course-adjustment-apply` | 页面有选课、换班、放弃修读、退课等申请入口；在点击任何申请前停下 |
| 选课结果查询 | `/for-std/course-take-query` | 识别学期与结果区；缴费、缴费单打印均不点 |
| 培养方案 | `/for-std/program` | 仅到达入口，内容未验证 |
| 全校开课查询 | `/for-std/lesson-search` | 可见课程、教师、院系等筛选结构；本次未执行搜索 |
| 全校培养方案查询 | `/for-std/program-search` | 可见查询与详情入口；本次未打开详情或批量打印 |

实际查询需求先确认课程/专业与学年，再读取所需的最小范围；未适配页面不伪称有通用 `read_page` 工具。以评课社区辅助比较时，分开列出课程安排事实与社区评价。

## 考试、证明和学业申请

| 菜单 | 已观察路径 | 流程与边界 |
| --- | --- | --- |
| 入学考试成绩 | `/for-std/std-enter-grade` | 到达入口，未读取具体分数 |
| 体测成绩查询 | `/for-std/sport-grade` | 到达查询页，未读取具体分数 |
| 考试信息 | `/for-std/exam-arrange` | 到达入口；考试安排需当次核实 |
| 缓考申请 | `/for-std/exam-delay-apply` | 页面有提示控件；不点含义不明的“确定”或“不再提示” |
| 开学补考申请 | `/for-std/make-up-exam-apply` | 停在列表；不新建或取消 |
| 免修考试申请 | `/for-std/exempt-exam-apply` | 停在列表；不新建或取消 |
| 电子证明导出 | `/for-std/school-report-print` | 页面代码包含国内/出国用途、接收单位/邮箱等字段；未验证可见填写流程，不生成、打印、邮件发送 |
| 课程替代申请 | `/for-std/course-substitute-apply` | 可见申请入口与替代课程库入口；不添加申请 |
| 转专业申请 | `/for-std/change-major-apply` | 仅到达入口；开放期和材料未核实 |
| 休学申请 | `/for-std/leaving-school-apply` | 停在“立即申请”之前 |
| 复学申请 | `/for-std/return-school-apply` | 仅到达入口；材料未核实 |

用户要准备申请时：先读当前校方通知和开放期；收集其指定课程、用途或事由；在本地整理材料清单。上述材料字段不是完整必填清单，不补造期限、审批结论或资格。业务预览在新建前结束，不进入真实提交链。

## 科研、竞赛、实习、毕业与学分

| 菜单 | 已观察路径 | 本次证据/停止点 |
| --- | --- | --- |
| 大研选题 | `/for-std/research-plan-selection` | 仅到达入口，不选择课题 |
| 大研中期申请 | `/for-std/research-plan-interim` | 功能未开放 |
| 大研结题申请 | `/for-std/research-plan-defense` | 功能未开放 |
| 大创选题 | `/for-std/startup-plan-selection` | 仅到达入口，不选择课题 |
| 大创提交中期检查 | `/for-std/startup-plan-flow` | 停在提交入口之前 |
| 境外科研项目开题申请 | `/for-std/oversea-research-topic` | 列表有新建、修改、撤回、提交、打印等控件；均未使用 |
| 竞赛成果登记 | `/for-std/competition-achievement` | 停在新建之前，不登记、不改动 |
| 竞赛绩点选择 | `/for-std/competition-grade-select` | 有提示控件，未确认或选择 |
| 我的实习 | `/for-std/practice-plan` | 实习计划上报页入口，未上报 |
| 毕业论文课题申报 | `/for-std/thesis-topic` | 仅进入申报页，不新建 |
| 毕业论文选题 | `/for-std/thesis-selection` | 仅进入选题页，不选择 |
| 开题中期评阅终稿 | `/for-std/thesis-flow` | 出现报告、答辩安排等入口；终稿暂未开放，未上传或打印 |
| 毕业申请 | `/for-std/graduate-audit-new-std` | 提示未开放或账号不在毕业名单内，停止 |
| 辅修申请 | `/for-std/non-major-apply` | 功能未开放 |
| 交流学分转换申请 | `/for-std/exchange-credit-apply` | 停在添加申请、确认进入成绩库之前 |

共同流程：从菜单定位业务 → 读当前开放状态 → 如用户要查询，只取其本人指定项目的现有记录 → 归纳待准备材料与未知事项 → 在新增记录/变更状态之前停止。以上深层详情、上传格式、材料必填项和审核链均未实测，不写成已验证操作步骤。

本次菜单还列出放弃成绩、学籍异动、大类分流、英才班、导师变更等入口；未进入的项目只算菜单发现。不要为补齐目录而自动进入确认毕业、退出项目或撤销类地址。
