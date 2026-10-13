from typing import Any
import asyncio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from . import client,operations
mcp=FastMCP('school-mcp-ustc-drive',instructions='科大云盘：先列资料库，再按用户指定库和目录查询、下载。上传、建文件夹、移动先准备预览再授权执行并回读核验，不覆盖同名项，不自动遍历所有文件，不创建分享链接，不删除。未知结果仅查询原计划。')
READ=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=True)
LOCAL=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=True)

@mcp.tool(annotations=READ)
async def school_drive_check_connection()-> dict[str, Any]:
    """核对科大云盘连接，列当前账号资料库和权限，不读取库内文件内容。"""
    return await asyncio.to_thread(client.libraries)

@mcp.tool(annotations=READ)
async def school_drive_list_files(repo_id:str,path:str='/',keyword:str='',limit:int=50)-> dict[str, Any]:
    """查询指定资料库某个目录的直接子项，可按文件名筛选；不递归全盘搜索。"""
    return await asyncio.to_thread(client.listing, repo_id,path,keyword,limit)

@mcp.tool(annotations=LOCAL)
async def school_drive_download(repo_id:str,path:str)-> dict[str, Any]:
    """下载指定库内文件到本机专用目录，最大 50 MiB，不打开或执行。"""
    return await asyncio.to_thread(client.download, repo_id,path)

@mcp.tool(annotations=LOCAL)
async def school_drive_prepare(operation:str,repo_id:str,path:str='/',name:str='',destination:str='',files:list[str]|None=None)-> dict[str, Any]:
    """准备 mkdir 建文件夹、upload 上传、move 同库移动。路径为库内 / 开头路径；files 为已选本机材料；同名项不覆盖。"""
    return await asyncio.to_thread(operations.prepare, operation,repo_id,path,name,destination,files)

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=True,idempotentHint=False,openWorldHint=True))
async def school_drive_execute(plan_id:str,content_sha256:str,authorized:bool=False)-> dict[str, Any]:
    """按用户授权执行原计划一次并核对结果；不会自动重发。"""
    return await asyncio.to_thread(operations.execute, plan_id,content_sha256,authorized)

@mcp.tool(annotations=READ)
async def school_drive_status(plan_id:str,verify:bool=True)-> dict[str, Any]:
    """查询原计划，并在结果未知时只读回查；state=verified 表示核对一致。"""
    return await asyncio.to_thread(operations.status, plan_id,verify)

def run():mcp.run(transport='stdio')

from ..runtime_version import install as install_runtime_status
install_runtime_status(mcp, 'drive')
