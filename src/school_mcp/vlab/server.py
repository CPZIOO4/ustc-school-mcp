from typing import Any
import asyncio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from . import client, jobs
mcp = FastMCP('school-mcp-ustc-vlab', instructions='只操作当前账号的 Vlab 虚拟机。先列虚拟机并检查环境，命令或上传先 prepare_task 展示目标、用户、命令与材料，再由用户授权执行。执行在虚拟机用户权限下，不是沙箱；结果未知只查询原计划。密钥自动从官方已有密钥页取得并本机加密，不进入聊天。')
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)

@mcp.tool(annotations=READ)
async def school_vlab_check_connection() -> dict[str, Any]:
    """核实 Vlab 学校会话并列当前账号机器；不连接桌面或创建虚拟机。"""
    return await asyncio.to_thread(client.machines)

@mcp.tool(annotations=READ)
async def school_vlab_environment(vm_id: int, username: str = 'vlab') -> dict[str, Any]:
    """检查该 VM 用户、系统、Python/Git/Bash 和磁盘；仅执行固定只读命令。"""
    return await asyncio.to_thread(client.environment, vm_id, username)

@mcp.tool(annotations=LOCAL)
async def school_vlab_prepare_task(vm_id: int, command: str = '', files: list[str] | None = None, username: str = 'vlab', timeout: int = 120) -> dict[str, Any]:
    """准备命令与上传文件，不执行。files 为用户选择的本机材料；上传后位于任务目录 inputs/。command 为空表示仅上传。超时 1–600 秒。"""
    return await asyncio.to_thread(jobs.prepare, vm_id, command, files, username, timeout)

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
async def school_vlab_execute(plan_id: str, content_sha256: str, authorized: bool = False) -> dict[str, Any]:
    """一次执行已准备且获授权的 VM 任务；异步运行，随后 status 查询，不自动重试。"""
    return await asyncio.to_thread(jobs.execute, plan_id, content_sha256, authorized)

@mcp.tool(annotations=READ)
async def school_vlab_status(plan_id: str, verify: bool = True, wait_seconds: int = 0) -> dict[str, Any]:
    """读取原任务结束回执；可等待 0–20 秒。succeeded 才表示命令成功，非零退出或超时不算成功。"""
    return await asyncio.to_thread(jobs.status, plan_id, verify, wait_seconds)

@mcp.tool(annotations=READ)
async def school_vlab_files(plan_id: str, relative: str = '') -> dict[str, Any]:
    """列出当前任务目录中的结果文件，不遍历其他虚拟机目录。"""
    return await asyncio.to_thread(jobs.files, plan_id, relative)

@mcp.tool(annotations=LOCAL)
async def school_vlab_download(plan_id: str, relative: str) -> dict[str, Any]:
    """下载本任务目录中的指定结果文件到本机，最大 50 MiB。"""
    return await asyncio.to_thread(jobs.download, plan_id, relative)

def run():
    mcp.run(transport='stdio')

from ..runtime_version import install as install_runtime_status
install_runtime_status(mcp, 'vlab')
