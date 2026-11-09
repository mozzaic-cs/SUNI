"""Is SUNI busy? One answer for anything that must not interrupt her.

updater.in_flight() looks like this answer and is not: only scheduled runs
count themselves there, so a person mid-conversation reads as idle. Every turn
goes through orchestrator._safe_run, and that is where turns are counted here.

Busy means any of:
  - a turn running now, or one that finished less than QUIET_SECONDS ago
    (a conversation is a series of turns; the gap between two is not idle);
  - a meeting being recorded, by anyone — a restart would cut it short;
  - a code_task run holding its project lock;
  - a scheduled run in flight.
"""
from __future__ import annotations

import threading
import time

QUIET_SECONDS = 300

_mu = threading.Lock()
_turns = {"active": 0, "last_end": 0.0}


def turn_started() -> None:
    with _mu:
        _turns["active"] += 1


def turn_finished() -> None:
    with _mu:
        _turns["active"] = max(0, _turns["active"] - 1)
        _turns["last_end"] = time.monotonic()


def busy_reasons(quiet_seconds: int = QUIET_SECONDS) -> list[str]:
    """Why SUNI is not idle; an empty list means she is. A check that cannot
    be read counts as busy: not knowing is not the same as idle."""
    out: list[str] = []
    with _mu:
        active, last_end = _turns["active"], _turns["last_end"]
    if active:
        out.append(f"{active} turn(s) running")
    elif last_end and time.monotonic() - last_end < quiet_seconds:
        out.append("a turn finished less than %d min ago" % max(1, quiet_seconds // 60))
    try:
        from .. import meetings
        if meetings._active:
            out.append("a meeting is being recorded")
    except Exception:      # noqa: BLE001
        out.append("could not tell whether a meeting is being recorded")
    try:
        from ..tools import code_project
        if any(lock.locked() for lock in list(code_project._locks.values())):
            out.append("a code task is running")
    except Exception:      # noqa: BLE001
        out.append("could not tell whether a code task is running")
    try:
        from .. import updater
        if updater.in_flight():
            out.append("a scheduled run is in flight")
    except Exception:      # noqa: BLE001
        out.append("could not tell whether a scheduled run is in flight")
    return out
