"""Start a hidden child outside a Windows caller's kill-on-close job object."""
from __future__ import annotations

import os
import subprocess


def spawn(args, *, cwd, env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
    flags = 0
    if os.name == 'nt':
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB
    # Do not fall back to a child that can silently die when the launcher exits.
    # If the host disallows breakaway, report launch failure instead.
    return subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=stdout, stderr=stderr, creationflags=flags,
                            start_new_session=os.name != 'nt')
