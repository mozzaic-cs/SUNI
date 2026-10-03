"""Starting other programs without putting a window on somebody's screen.

On Windows, launching a console program from a process that is itself running
without a visible console — a service, a pythonw host, or anything started from
a shortcut rather than a terminal — gives the child a console of its own, and
that console is a black window that appears in front of whatever the person was
doing. During a Teams meeting, SUNI taking a screenshot every two seconds put a
command window over the slides she was being asked to capture.

CREATE_NO_WINDOW is the flag that says "run it, but do not give it a window".
It exists only on Windows, so asking for it anywhere else is an AttributeError
rather than a no-op, which is why this is a function and not a constant.

Everything here starts subprocesses through one of these two helpers, so the
answer lives in one place rather than in thirteen call sites that each have to
remember.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys

__all__ = ["no_window", "run", "exec_", "shell", "CREATE_NO_WINDOW",
           "own_group", "kill_tree"]

# 0 on every platform that has no such concept, so it can be OR-ed into other
# creation flags without a branch at the call site.
CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def no_window(**kwargs):
    """Keyword arguments that keep a child process off the screen.

    Merges with creationflags the caller already wants rather than replacing
    them, because a caller that asked for DETACHED_PROCESS meant it.
    """
    if not CREATE_NO_WINDOW:
        return kwargs
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
    return kwargs


def run(*args, **kwargs):
    """subprocess.run, minus the window. A drop-in in every other respect."""
    return subprocess.run(*args, **no_window(**kwargs))


async def exec_(*args, **kwargs):
    """asyncio.create_subprocess_exec, minus the window."""
    return await asyncio.create_subprocess_exec(*args, **no_window(**kwargs))


def own_group(**kwargs):
    """Keyword arguments that make the child the head of its own process group
    on POSIX, so kill_tree can take its descendants with it. Windows needs
    nothing here: taskkill /T walks the tree by parent id."""
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    return kwargs


def kill_tree(proc) -> None:
    """Kill a child AND everything it started, then return without waiting.

    proc.kill() alone is not enough for the agent CLIs. Measured 2026-10-03:
    cancelling a Claude Code run left claude.exe running 4s later, because
    nothing killed it on cancellation at all, and on timeout only the direct
    child dies while the shells and test runners it launched keep going. With
    edit rights that means pressing stop does not stop the edits.
    """
    if proc is None or proc.returncode is not None:
        return
    pid = proc.pid
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, **no_window())
        else:
            import os
            import signal
            try:
                if os.getpgid(pid) == pid:      # started with own_group()
                    os.killpg(pid, signal.SIGKILL)
                    return
            except (ProcessLookupError, PermissionError):
                pass
            proc.kill()
    except Exception:       # noqa: BLE001 — best effort; never mask the caller's error
        try:
            proc.kill()
        except Exception:   # noqa: BLE001
            pass


async def shell(cmd, **kwargs):
    """asyncio.create_subprocess_shell, minus the window.

    This one matters most: a shell command is the case where Windows is most
    willing to hand out a console, and it is the path SUNI takes every time she
    is asked to run something.
    """
    return await asyncio.create_subprocess_shell(cmd, **no_window(**kwargs))
