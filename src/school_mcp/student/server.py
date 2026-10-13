from typing import Any
import asyncio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from . import client, applications

mcp = FastMCP('school-mcp-ustc-student', instructions='学工奖学金查询及优秀学生奖学金申请。先查列表取得 application_reference，再读 application_guide，一次补齐真实信息，prepare_application冻结完整预览，用户已有具体授权时submit_application，最后application_status。打开表单会建草稿，字段自动保存；只读查询绝不打开表单。综合表现至少150字，不编造经历；个人表单无金银铜等级选择。未知结果只查原计划，不重复提交。其他奖学金、附件及修改已提交申请未适配。')
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

@mcp.tool(annotations=READ)
async def school_student_check_connection() -> dict[str, Any]:
    """真实核对学工系统当前会话。"""
    return await asyncio.to_thread(client.check)

@mcp.tool(annotations=READ)
async def school_student_scholarships(kind: str = 'open', keyword: str = '') -> dict[str, Any]:
    """查询已开通 open、已申请 applied、获奖 awarded 的奖学金，返回原始状态字段与详情引用。"""
    return await asyncio.to_thread(client.scholarships, kind, keyword)

@mcp.tool(annotations=READ)
async def school_student_notices(keyword: str = '', limit: int = 20) -> dict[str, Any]:
    """查询学工首页当前可见通知，详情使用返回的 reference。"""
    return await asyncio.to_thread(client.notices, keyword, limit)

@mcp.tool(annotations=READ)
async def school_student_read(reference: str) -> dict[str, Any]:
    """读取当前账号已发现的通知或奖学金条件。仅资料，不提交申请。"""
    return await asyncio.to_thread(client.read, reference)

@mcp.tool(annotations=READ)
async def school_student_application_guide() -> dict[str, Any]:
    """离线返回已适配优秀学生奖学金的字段、必填条件、活动选项和申请边界；不打开或创建草稿。"""
    return applications.guide()

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
async def school_student_prepare_application(application_reference: str, fields: dict[str, str],
        activities: list[str] | None = None, requested_level: str = '铜奖') -> dict[str, Any]:
    """仅在本机校验并冻结申请预览，不打开学校表单。reference来自列表；fields按guide填写；activities为实际参加活动，参赛不等于获奖。缺项集中返回，statement至少150字。"""
    return await asyncio.to_thread(applications.prepare, application_reference, fields, activities, requested_level)

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
async def school_student_submit_application(plan_id: str, content_sha256: str, authorized: bool = False) -> dict[str, Any]:
    """按已授权原计划一次执行：查状态→打开/新建草稿→填写保存→重读核对→提交→回查。个人已提交时停止不覆盖；不接受自由URL或代码；超时查原计划。"""
    return await asyncio.to_thread(applications.submit, plan_id, content_sha256, authorized)

@mcp.tool(annotations=READ)
async def school_student_application_status(plan_id: str, verify: bool = True) -> dict[str, Any]:
    """查原计划，结果未知时只读官方已申报列表；个人已提交不等于获奖。不会打开表单、填字段或再次提交。"""
    return await asyncio.to_thread(applications.status, plan_id, verify)

def run():
    mcp.run(transport='stdio')

from ..runtime_version import install as install_runtime_status
install_runtime_status(mcp, 'student')
