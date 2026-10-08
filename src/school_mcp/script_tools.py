"""Register service-scoped workflow tools without exposing an arbitrary script executor."""
from __future__ import annotations

import asyncio
from typing import Any
from mcp.types import ToolAnnotations

from . import script_jobs as jobs
from .script_workflows import (Schedule, MailRequest, BBSubmit, JWWatch,
                               schedule_mail, schedule_bb, schedule_jw,
                               plan_timetable, prepare_bb_by_name)
from .jw.planner import Constraints

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def install(mcp, service):
    @mcp.tool(name=f'school_{service}_script_status', annotations=READ)
    def script_status(job_id: str, include_results: bool = False) -> dict[str, Any]:
        """查询固定脚本任务状态；默认仅状态、计数结果名与原操作ID，include_results才返回回执详情。review/interrupted时只核实原操作，不另建任务重试。后台进程不依赖AI，但电脑重启后不会自动恢复。"""
        return jobs.view(job_id, service, include_results=include_results)

    @mcp.tool(name=f'school_{service}_cancel_script', annotations=LOCAL)
    def cancel_script(job_id: str) -> dict[str, Any]:
        """停止后续轮询和写入；已开始的外部操作可能完成，不能撤回。先绑定当前服务账号，再持久化取消标记。请继续核实原操作回执。"""
        jobs.view(job_id, service)
        return jobs.JobStore().cancel(job_id)

    if service == 'mail':
        @mcp.tool(name='school_mail_schedule_workflow', annotations=WRITE)
        def mail_workflow(request: MailRequest, schedule: Schedule, authorized: bool = False) -> dict[str, Any]:
            """启动不依赖AI的后台固定流程：mail_send发送冻结草稿（可保存已发送副本）；mail_replies按Message-ID跟踪回复；mail_watch限定范围新邮件计数/UID；mail_archive按已确认规则归档。发送/归档须已有用户明确授权再填authorized=true。默认只观察创建任务之后的新邮件；include_existing须指定since。规则改变、UIDVALIDITY改变、验证码/人工验证或未知写入结果停止。认证邮件/规则冲突/未匹配不自动移动。时间含时区、明确结束时间、轮询至少60秒；不创建任意脚本或模型任务。"""
            return schedule_mail(request, schedule, authorized)

    if service == 'bb':
        @mcp.tool(name='school_bb_prepare_named_submission', annotations=LOCAL.model_copy(update={'openWorldHint': True, 'idempotentHint': False}))
        async def named_submission(course_keyword: str, assignment_keyword: str, files: list[str] | None = None,
                                   term: str = '', comment: str = '', resubmit: bool = False,
                                   reuse_previous_files: bool = False, allow_late: bool = False) -> dict[str, Any]:
            """按课程/作业关键词串起唯一定位、要求检查和材料冻结。多匹配/列表截断返回needs_input，不猜目标。不上传或提交。明确重交/迟交才能设置对应参数；成功后可立即调用submit_assignment，或用返回ID和摘要创建定时脚本。"""
            return await asyncio.to_thread(prepare_bb_by_name, course_keyword, assignment_keyword, files, term,
                                           comment, resubmit, reuse_previous_files, allow_late)

        @mcp.tool(name='school_bb_schedule_submission', annotations=WRITE)
        def bb_workflow(request: BBSubmit, schedule: Schedule, authorized: bool = False) -> dict[str, Any]:
            """授权后后台执行固定作业准备记录并核验新尝试和附件哈希；无需AI在线。结束时间须在准备24小时有效期内且不超出未获准的迟交边界。会话、要求或历史改变停止；结果未知仅查询原回执，不重交。"""
            return schedule_bb(request, schedule, authorized)

    if service == 'jw':
        @mcp.tool(name='school_jw_build_timetable', annotations=READ)
        def build_timetable(semester_id: int, keywords: list[str], constraints: Constraints) -> dict[str, Any]:
            """一次调用读取已有课表、分页查询1–5个官方课程关键词并排课。保留已选课程，最多25候选/每词5页；截断、元数据不足返回needs_input，不悄悄丢弃课程。仅生成候选，不选退课。"""
            return plan_timetable(semester_id, keywords, constraints)

        @mcp.tool(name='school_jw_schedule_enrollment_watch', annotations=LOCAL.model_copy(update={'openWorldHint': True, 'idempotentHint': False}))
        def jw_workflow(request: JWWatch, schedule: Schedule) -> dict[str, Any]:
            """后台监控指定学期/课堂的选课批次和余量。当前没有已验证的选退课写接口：开放且余量满足后返回live_selection_contract_not_verified并停止；开放但满员继续监控，容量未知停止，不承诺抢课，不创建选退课记录。容量未知保持unknown。"""
            return schedule_jw(request, schedule)
