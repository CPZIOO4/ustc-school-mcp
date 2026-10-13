# 统一缺项提示

财务预检、BB 按名称准备作业、教务排课/规划上下文和集市缺字段的 `needs_input` 分支返回 `input_guidance`。参数类型错误仍由 MCP schema 拒绝；本协议不代表所有工具或所有异常已经适配。

1. 按 `fields` 一次汇总问题，保留每项 `path`、`question` 和 `expected_format`。path是工具参数中的位置；数组还可带 `selector`，例如财务 `/request/facts` 加 `{"field":"purpose"}`，表示修改该事项，不能覆盖整组已知信息。
2. 根据 `source` 处理：`user` 是用户选择或提供；`tool_result` 必须从已有结果取标识或分类；`official_data` 需要核对学校原数据，缺学分/课表不能用猜测填补；`verified_document` 来自实际核对的材料；`manual_review` 需要人工确认重复、分摊等情况。不要把后四种全部当成随便填一个文本的提问。
3. `resume.tool` 给出后续工具。`resume.arguments` 只包含已知且适合返回的少量参数，`arguments_complete=false` 和 `ready_to_call=false` 表示**不能直接调用**。从原请求恢复 `preserve_arguments`，合并用户答案或核实结果后，再按该工具 schema 形成完整请求。原参数丢失时先恢复原记录或询问缺少的具体信息，不猜文件、正文或金额。
4. BB 教学班歧义会先转到 `school_bb_list_assignments`，选择已有 `course_id` 后再找作业；作业多匹配且列表完整时转到 `school_bb_prepare_submission`，保留原文件、附言、重交和迟交选项。原请求中的文件与授权在只读步骤之间也要保留。截断列表不自动选第一项。
5. 财务重新调用须提交完整 `request`；工具不会保存上一轮输入。集市保留已填写描述、价格、图片等，不用缺项提示中的格式说明作真实内容。排课保留原约束及已选课，不为消除问题而移除必留课程。

此协议不提供通用“自动执行”入口。`mutation_authorized=false` 表示补齐提示本身不产生授权；保持原业务的预览、已有授权与结果核验流程。
