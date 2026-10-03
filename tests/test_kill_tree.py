"""proc.kill_tree must take the child's descendants with it.

The agent CLIs (Claude Code, Codex) start shells and test runners of their
own. Measured 2026-10-03: cancelling a Claude Code run left claude.exe alive,
so pressing stop did not stop the work. This uses a plain Python child that
starts a grandchild, so it needs neither CLI installed.

No pipes are shared with the grandchild: the pid comes back through a file.
An inherited stdout pipe makes wait() block until the grandchild exits, and
an earlier version of this test read that as "plain kill() works" - a
control that passed for the wrong reason.
"""
import asyncio
import subprocess
import sys
import time

from suni import proc as _proc

_CHILD = r"""
import subprocess, sys, time
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
open(sys.argv[1], "w").write(str(g.pid))
time.sleep(120)
"""


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True).stdout
        return f'"{pid}"' in out
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:                                   # an unreaped zombie is not alive
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return True


async def _spawn(tmp_path, group: bool):
    pidfile = tmp_path / "grandchild.pid"
    kw = _proc.own_group() if group else {}
    p = await _proc.exec_(sys.executable, "-c", _CHILD, str(pidfile),
                          stdin=asyncio.subprocess.DEVNULL,
                          stdout=asyncio.subprocess.DEVNULL,
                          stderr=asyncio.subprocess.DEVNULL, **kw)
    deadline = time.monotonic() + 20
    while not (pidfile.exists() and pidfile.read_text().strip()):
        assert time.monotonic() < deadline, "child never reported its grandchild"
        await asyncio.sleep(0.1)
    return p, int(pidfile.read_text())


async def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while _alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    return not _alive(pid)


def _cleanup(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        import os, signal
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_plain_kill_leaves_the_grandchild(tmp_path):
    """The control: proves the scenario actually reproduces the bug, so the
    test below cannot pass vacuously."""
    async def scenario():
        p, g = await _spawn(tmp_path, group=True)
        try:
            p.kill()
            await asyncio.wait_for(p.wait(), 10)
            await asyncio.sleep(1)
            assert _alive(g), "plain kill() took the grandchild too - control is invalid"
        finally:
            _cleanup(g)
    asyncio.run(scenario())


def test_kill_tree_takes_the_grandchild(tmp_path):
    async def scenario():
        p, g = await _spawn(tmp_path, group=True)
        try:
            assert _alive(p.pid) and _alive(g)
            _proc.kill_tree(p)
            await asyncio.wait_for(p.wait(), 10)
            assert await _gone_within(g, 10), "grandchild survived kill_tree"
        finally:
            _cleanup(g)
    asyncio.run(scenario())


def test_kill_tree_is_a_noop_on_a_finished_process():
    async def scenario():
        p = await _proc.exec_(sys.executable, "-c", "pass", **_proc.own_group())
        await p.wait()
        _proc.kill_tree(p)          # must not raise
    asyncio.run(scenario())
