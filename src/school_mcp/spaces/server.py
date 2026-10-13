from typing import Any
import asyncio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from . import client, reservations
mcp = FastMCP('school-mcp-ustc-spaces', instructions='西区图书馆学习空间。先读取分类，再按日期和分类查询资源及占用时段。查询不占位；不能把空占用列表直接当作满足所有预约条件。中区和高新区使用不同系统，不能混用资源编号。')
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

@mcp.tool(annotations=READ)
async def school_spaces_check_connection() -> dict[str, Any]:
    """核实西区图书馆学习空间当前连接。"""
    return await asyncio.to_thread(client.check)

@mcp.tool(annotations=READ)
async def school_spaces_categories() -> dict[str, Any]:
    """列出实际空间分类及其 kindId，例如小型讨论室和云面试间。"""
    return await asyncio.to_thread(client.categories)

@mcp.tool(annotations=READ)
async def school_spaces_availability(date: str, kind_id: int, page: int = 1, limit: int = 20) -> dict[str, Any]:
    """按 YYYY-MM-DD 日期和 categories 返回的 kindId 查询开放与占用时段，不预约。"""
    return await asyncio.to_thread(client.availability, date, kind_id, page, limit)

@mcp.tool(annotations=READ)
async def school_spaces_reservations(date: str, page: int = 1, limit: int = 20) -> dict[str, Any]:
    """按日期查询本人预约，返回 uuid、时间、资源和原始状态；不查询其他人的身份信息。"""
    return await asyncio.to_thread(reservations.mine, date,page,limit)

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
async def school_spaces_prepare(kind_id:int,dev_id:int,begin:str,end:str,title:str,participants:list[str]|None=None)-> dict[str, Any]:
    """准备预约并核对资源、时间、人数、参与者账号。北京时间 YYYY-MM-DD HH:MM，自动包含本人，不占位。"""
    return await asyncio.to_thread(reservations.prepare, kind_id,dev_id,begin,end,title,participants)

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
async def school_spaces_prepare_cancel(date:str,reservation_id:str)-> dict[str, Any]:
    """准备取消本人预约，reservation_id 使用本人列表 uuid；只保存预览。"""
    return await asyncio.to_thread(reservations.prepare_cancel, date,reservation_id)

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
async def school_spaces_execute(plan_id:str,content_sha256:str,authorized:bool=False)-> dict[str, Any]:
    """按授权一次执行原预约/取消计划，再从本人列表核对。验证码变化时停下，不重复提交。"""
    return await asyncio.to_thread(reservations.execute, plan_id,content_sha256,authorized)

@mcp.tool(annotations=READ)
async def school_spaces_status(plan_id:str,verify:bool=True)-> dict[str, Any]:
    """查询原计划；结果未知时只读核对，不能重发。verified 表示记录匹配，审核状态另看预约回执。"""
    return await asyncio.to_thread(reservations.status, plan_id,verify)

def run():
    mcp.run(transport='stdio')

from ..runtime_version import install as install_runtime_status
install_runtime_status(mcp, 'spaces')
