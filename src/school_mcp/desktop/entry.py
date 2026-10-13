"""Frozen app entry; also dispatches existing hidden worker invocations."""
import multiprocessing
import os
import sys


def main():
    multiprocessing.freeze_support()
    for name in ('stdout', 'stderr'):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, 'w', encoding='utf-8'))
    # Existing schedulers call the executable with Python's -X/-m convention.
    argv = sys.argv[1:]
    if argv[:2] == ['-X', 'utf8']:
        argv = argv[2:]
    if argv[:2] == ['-m', 'school_mcp']:
        sys.argv = [sys.argv[0], *argv[2:]]
        from school_mcp.__main__ import main as cli
        return cli()
    from school_mcp.desktop.server import main as desktop
    try:
        desktop(argv)
    except Exception as exc:
        from school_mcp.safety_log import record
        incident = record(code='startup_failed', stage='startup', exc=exc, outcome='not_started')
        # No raw traceback that could include a local credential or request URL.
        if os.name == 'nt' and '--native-stdio' not in argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, '学校助手启动失败。请检查数据目录权限，或稍后重新打开。\n错误编号：' + incident['error_id'] +
                ('\n诊断日志已保存在本机。' if incident['diagnostic_saved'] else '\n日志也无法写入，请检查磁盘空间和目录权限。'), '学校助手', 0x10)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
