"""Background login lifecycle shared by the school adapters."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from .mail.config import local_dir

SERVICES = ("bb", "jw", "library", "nan7", "young")
TERMINAL = {"connected", "cancelled", "error", "waiting_for_verification"}


def status_tool(adapter: str) -> str:
    return "school_bb_auth_status" if adapter == "bb" else f"school_{adapter}_status"


def progress_path(adapter: str) -> Path:
    name = "nan7-login-status.json" if adapter == "nan7" else f"{adapter}.login-status.json"
    return local_dir() / name


def worker_pid(adapter: str) -> int | None:
    try:
        pid = int((local_dir() / f"{adapter}-login.pid").read_text(encoding="utf-8-sig").strip())
        return pid if pid > 0 else None
    except (OSError, ValueError):
        return None


def running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False



def progress(adapter: str) -> dict:
    """Historical connected state is never evidence of a current live connection."""
    try:
        data = json.loads(progress_path(adapter).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("stage"), str):
            data = None
    except (OSError, ValueError):
        data = None
    pid = worker_pid(adapter)
    alive = running(pid) if pid else None
    result = {"login_progress": data, "login_running": alive, "network_checked": False,
              "connection_state": "unchecked", "status_tool": status_tool(adapter),
              "check_tool": f"school_{adapter}_check_connection"}
    if data and data["stage"] not in TERMINAL and alive is False:
        result["login_progress"] = {**data, "last_reported_stage": data["stage"], "stage": "interrupted",
            "detail": "登录进程已退出，未报告成功。请重新连接；不要继续等待旧进度。"}
        result["next_step"] = f"调用 school_{adapter}_reconnect 重新启动后台登录。"
    elif data and data["stage"] == "waiting_for_verification":
        result["next_step"] = f"需要人工验证；明确选择后运行 python -m school_mcp {adapter}-login --headed。"
    elif alive:
        result["next_step"] = f"后台登录仍在运行，稍后调用 {status_tool(adapter)}；不要重复启动。"
    elif data and data["stage"] in {"error", "cancelled"}:
        result["next_step"] = f"上次登录未完成，请根据进度中的原因处理后调用 school_{adapter}_reconnect。"
    else:
        result["next_step"] = f"调用 school_{adapter}_check_connection 验证当前连接。"
    return result


@contextmanager
def launch_lock():
    """Short OS lock around the running-worker check and spawn, released on crash."""
    directory = local_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "login-launch.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def start_login(adapter: str, notify, error_type, force_identity_login: bool = False) -> dict:
    try:
        with launch_lock():
            return _start_login(adapter, notify, error_type, force_identity_login)
    except OSError:
        return {"started": False, "already_running": False, "status_tool": status_tool(adapter),
                "next_step": "登录启动暂时不可用：另一请求可能正在启动，或私人目录不可写。请稍后查看状态，不要连续重复启动。"}


def _start_login(adapter: str, notify, error_type, force_identity_login: bool = False) -> dict:
    directory = local_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # All adapters write the same remembered-device state. Serialize their login workers.
    for other in SERVICES:
        pid = worker_pid(other)
        if pid and running(pid):
            return {"started": False, "already_running": other == adapter, "busy_service": other,
                    "status_tool": status_tool(other),
                    "next_step": f"{other} 的登录进程仍在运行，先调用 {status_tool(other)} 查看进度，再继续。"}
    notify("starting", "已请求后台登录，正在恢复已保存的认证状态。")
    executable = Path(sys.executable)
    if os.name == "nt" and executable.with_name("pythonw.exe").exists():
        executable = executable.with_name("pythonw.exe")
    arguments = [str(executable), "-m", "school_mcp", f"{adapter}-login"]
    if force_identity_login:
        arguments.append("--force-identity-login")
    # A prior manually headed command must not make an MCP reconnect pop up a window.
    environment = {**os.environ, "SCHOOL_MCP_LOCAL_DIR": str(directory), "PYTHONUTF8": "1", "SCHOOL_MCP_BROWSER_HEADED": "0"}
    try:
        with (directory / f"{adapter}-login.log").open("ab") as output, (directory / f"{adapter}-login.error.log").open("ab") as errors:
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                env=environment, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError:
        notify("error", "后台登录程序启动失败，请检查 Python 环境和本地目录权限。")
        raise error_type("后台登录程序启动失败；已有会话保留，请检查本地环境后重新连接。") from None
    try:
        (directory / f"{adapter}-login.pid").write_text(str(process.pid), encoding="utf-8")
    except OSError:
        process.terminate()
        process.wait(timeout=5)
        notify("error", "无法保存登录进程状态，本次新启动的进程已停止；请检查私人目录权限。")
        raise error_type("无法保存后台登录进程状态，请检查私人目录权限。") from None
    return {"started": True, "already_running": False, "headless": True, "status_tool": status_tool(adapter),
            "poll_after_seconds": 2,
            "next_step": f"调用 {status_tool(adapter)} 查看结果，完成后继续原查询；需要人工验证时会停止并报告。"}
