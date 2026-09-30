"""Starting other programs without putting a window on somebody's screen.

Asked to capture the slides of a Teams meeting every two seconds, SUNI did it —
and put a black command window over the slides she was capturing, repeatedly,
for the length of the meeting.

On Windows a console program launched from a process without a visible console
of its own gets one, and that console is a window in front of whatever the
person was doing. CREATE_NO_WINDOW is the flag that says otherwise. It exists
only on Windows, so it is applied through a helper rather than written at
thirteen call sites that each have to remember the platform check.

The test that earns its keep is the last one: it walks the source for anyone
spawning a process the direct way and fails, because a new call site added in a
year's time is exactly how this comes back.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

from suni import proc

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_flag_is_real_where_it_matters():
    if sys.platform == "win32":
        assert proc.CREATE_NO_WINDOW == subprocess.CREATE_NO_WINDOW
        assert proc.no_window()["creationflags"] & proc.CREATE_NO_WINDOW
    else:
        # Asking for it off Windows is an AttributeError, not a no-op, which is
        # why this is a function and not a constant.
        assert proc.CREATE_NO_WINDOW == 0
        assert proc.no_window() == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only flag")
def test_it_adds_to_the_caller_s_flags_rather_than_replacing_them():
    """A caller that asked for its own process group meant it."""
    got = proc.no_window(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    assert got["creationflags"] == (subprocess.CREATE_NEW_PROCESS_GROUP
                                    | subprocess.CREATE_NO_WINDOW)


def test_the_wrapper_still_runs_the_program():
    """A flag that silently stopped things working would be worse than a
    window."""
    r = proc.run([sys.executable, "-c", "print(42)"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "42"


def test_nothing_spawns_a_process_the_direct_way():
    """The check that survives this session. A new call site written the
    obvious way is how the windows come back, and nothing else would notice.
    """
    direct = re.compile(
        r"\b(subprocess\.(?:run|Popen|call|check_output|check_call)"
        r"|asyncio\.create_subprocess_(?:exec|shell))\s*\(")
    offenders = []
    for p in (ROOT / "suni").rglob("*.py"):
        if p.name == "proc.py":
            continue          # the one place allowed to call them directly
        try:
            src = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            src = p.read_text(encoding="utf-8-sig")
        for m in direct.finditer(src):
            line = src.count("\n", 0, m.start()) + 1
            offenders.append(f"{p.relative_to(ROOT)}:{line}: {m.group(1)}")
    assert not offenders, (
        "these start a process without going through suni.proc, so on Windows "
        "they will put a console window on screen:\n  " + "\n  ".join(offenders)
    )
