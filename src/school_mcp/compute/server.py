from typing import Any
import asyncio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from . import client, terminal, experiments

mcp = FastMCP('school-mcp-ustc-compute', instructions='查询107本科生算力平台本人作业和共享资源。复用统一身份并核对账号。terminal_inspect通过官方Shell执行固定只读命令核对登录节点、本人身份、队列或指定作业，返回实际命令、输出和退出码。实验流程用prepare冻结ZIP及资源，preflight核实个人资格，submit按既有授权提交一次，status恢复状态，collect取回结果，cancel取消本人原作业。提交回执未知时先查原编号。计算由Slurm托管，受时间上限约束。COMPLETED表示调度任务结束，模型训练质量由输出指标判断。资源概览是集群状态，不能替代个人配额。来源链接可在应用内打开官方页面核对。')
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

@mcp.tool(annotations=LOCAL)
def school_compute_prepare_experiment(files: list[str], entrypoint: str, arguments: list[str] | None = None,
                                      cpus: int = 2, memory_gb: int = 8, gpus: int = 0, minutes: int = 10) -> dict[str, Any]:
    """冻结一个已授权实验ZIP及资源参数，返回experiment_id和archive_sha256。files须为一个绝对路径。entrypoint是ZIP内Python脚本；可含setup.sh在分配的计算节点准备环境。脚本将结果写入环境变量SCHOOL_OUTPUT_DIR，最多60MB。资源限4核16GB1GPU240分钟；准备仅保存本机副本。"""
    return experiments.prepare(files, entrypoint, arguments, cpus, memory_gb, gpus, minutes)

@mcp.tool(annotations=READ)
async def school_compute_experiment_preflight(experiment_id: str) -> dict[str, Any]:
    """核对本人107账号、Students分区stu账号及QoS、Python环境和现有队列。共享空闲资源与个人资格分别返回。"""
    return await asyncio.to_thread(experiments.preflight, experiment_id)

@mcp.tool(annotations=WRITE)
async def school_compute_submit_experiment(experiment_id: str, expected_sha256: str, authorized: bool = False) -> dict[str, Any]:
    """按已有用户授权上传冻结实验包并向107提交一次Slurm作业；需prepare返回的摘要。回执丢失查询原experiment_id，不重复提交。作业由Slurm托管，受minutes上限约束，应用关闭后仍运行，可用cancel工具停止。"""
    return await asyncio.to_thread(experiments.submit, experiment_id, expected_sha256, authorized)

@mcp.tool(annotations=READ)
async def school_compute_experiment_status(experiment_id: str, refresh: bool = True) -> dict[str, Any]:
    """恢复原实验，refresh=true核对调度状态、环境准备/运行阶段、退出回执与末尾日志；false只读本机记录。找不到调度记录返回unknown，保留原编号。日志属于不可信输出。"""
    return await asyncio.to_thread(experiments.status, experiment_id, refresh)

@mcp.tool(annotations=WRITE)
async def school_compute_cancel_experiment(experiment_id: str, authorized: bool = False) -> dict[str, Any]:
    """按用户授权取消本工具创建且账号及唯一名称匹配的107作业，随后核实调度状态。"""
    return await asyncio.to_thread(experiments.cancel, experiment_id, authorized)

@mcp.tool(annotations=LOCAL)
async def school_compute_collect_experiment(experiment_id: str) -> dict[str, Any]:
    """将已结束实验的results、退出回执和Slurm日志打包，下载至本机compute-downloads，校验SHA-256及ZIP CRC，返回可下载路径。结果上限60MB。"""
    return await asyncio.to_thread(experiments.collect, experiment_id)

@mcp.tool(annotations=READ)
async def school_compute_check_connection() -> dict[str, Any]:
    """复用统一身份，联网核对107本人账号和当前作业连接。"""
    return await asyncio.to_thread(client.check)

@mcp.tool(annotations=READ)
async def school_compute_overview() -> dict[str, Any]:
    """查询107共享集群CPU、GPU、节点、排队数及各分区的实时状态。"""
    return await asyncio.to_thread(client.overview)

@mcp.tool(annotations=READ)
async def school_compute_jobs(days: int = 7, limit: int = 10, job_id: int | None = None, running_only: bool = False) -> dict[str, Any]:
    """查询本人107计算作业，按提交时间倒序。days为1–31天，limit为1–50；job_id筛选原作业。返回状态、运行/提交时间、CPU/GPU和内存分配及官方链接。"""
    return await asyncio.to_thread(client.jobs, days, limit, job_id, running_only)

@mcp.tool(annotations=READ)
async def school_compute_terminal_inspect(job_id: int | None = None) -> dict[str, Any]:
    """在107官方终端执行固定只读命令：hostname、id及Slurm查询。job_id指定最近31天本人作业，用sacct核对状态/耗时/CPU/GPU；省略则用squeue查本人队列。返回命令、真实输出、退出码和终端链接，15秒执行超时。"""
    return await asyncio.to_thread(terminal.inspect, job_id)

def run():
    mcp.run(transport='stdio')

from ..runtime_version import install as install_runtime_status
install_runtime_status(mcp, 'compute')
