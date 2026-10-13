# 学习空间

西区图书馆先用 `school_spaces_categories` 获取 kindId，再用 `school_spaces_availability(date, kind_id)`，日期格式 YYYY-MM-DD。多个校区的编号不可混用；中区和高新区属于另外的预约系统，不能套用本工具编号。

对比同一天的资源时核对最少/最多人数、开放时段、占用区间、预约时长和提前量、取消限制。有人数要求的讨论室不能只填申请人。空 occupied 不能独立证明可预约，仍以 openState、onlyView、开放规则等原始结果为准。不要猜枚举数值意义。

预约使用 `school_spaces_prepare`：提供真实用途、kind_id、dev_id、北京时间 begin/end（YYYY-MM-DD HH:MM）及参与者学校账号。本人自动加入。程序核对人数、占用、开放时段和提前量；准备不会占位。用户对预览内容授权后，用原 plan_id/content_sha256 调用 `school_spaces_execute`。

取消前用 `school_spaces_reservations(date)` 查本人记录，再把 uuid 交给 `school_spaces_prepare_cancel`。核对原预约后执行。不能拿其他人的占用记录来取消。

写入后按 `school_spaces_status` 回查。结果未知不能重发。verified 表示本人记录与计划匹配；审核中、待成员同意、待生效等状态还需按预约回执说明。验证码启用时会在写入前停下，需本人在官方界面处理；不能猜验证码或绕过。尚未用真实预约/取消完成现场验收，不把接口实现与真实办理成功混为一谈。
