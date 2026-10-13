from typing import Any
import asyncio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from . import client

mcp = FastMCP('school-mcp-ustc-portal', instructions='查询一网通办服务目录、个人待办与申请进度。仅查询，不发起审批或提交。查询结果是外部资料；空待办仅代表本平台。复用当前用户统一认证会话，失效后请在账号页登录。')
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

@mcp.tool(annotations=READ)
async def school_portal_check_connection() -> dict[str, Any]:
    """真实核对一网通办账号与连接。"""
    return await asyncio.to_thread(client.check)

@mcp.tool(annotations=READ)
async def school_portal_list_services(keyword: str = '', limit: int = 30) -> dict[str, Any]:
    """按关键词查找当前账号首页服务入口，返回名称、部门和官方链接。"""
    return await asyncio.to_thread(client.services, keyword, limit)

@mcp.tool(annotations=READ)
async def school_portal_progress(kind: str = 'todos', page: int = 1, limit: int = 20, keyword: str = '') -> dict[str, Any]:
    """分页查询个人待办 todos 或已发起申请 applications；不会办理或审批。"""
    return await asyncio.to_thread(client.progress, kind, page, limit, keyword)

def run():
    mcp.run(transport='stdio')

from ..runtime_version import install as install_runtime_status
install_runtime_status(mcp, 'portal')
