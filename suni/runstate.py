"""
A black box for the server process — when was SUNI last alive, and did it stop
cleanly?

This exists because of a real three-day outage. SUNI stopped at roughly 23:27 on
28 Aug 2026 and nothing recorded it: `startup.log` simply ends mid-stream, the
`[SHUTDOWN]` line the shutdown handler writes never appeared, and the daily
application log had already rotated. Reconstructing even the *time of death*
took the Windows Task Scheduler's `LastTaskResult`, and reconstructing the cause
was impossible because the relevant event log was disabled.

The lesson is not "log harder on the way out". A process that is killed cannot
log anything at all — no handler runs for SIGKILL or a Task Scheduler
termination, which is exactly what happened. So the record has to be written
*while running*, and read by the *next* start:

  • a heartbeat, rewritten every scheduler tick, says "alive at T";
  • a clean shutdown sets status="stopped" and a reason;
  • a start that finds status=="running" knows the previous run died without
    stopping, and reports when it was last seen.

That converts "it is down and nobody knows since when" into a timestamped line
in the log on the very next start. It cannot say WHO killed the process — no
in-process mechanism can — but "died between 23:27 and 23:28, unclean" is the
difference between a hypothesis and a fact.

Deliberately a plain JSON file, not a table. It is written on a timer for the
life of the process, it must survive the database being locked, corrupt or
mid-migration, and it holds nothing about any user — so it is not part of the
subject-access or erasure inventories, and must never grow a user-keyed field.

Every function here swallows its own errors. This is diagnostics: a failure to
write the black box must never take down the flight.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .logger import get_logger

_log = get_logger(__name__)

_PATH = Path("memory") / "runstate.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(state: dict) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:                      # diagnostics must not be fatal
        _log.debug("[RUNSTATE] could not write %s: %s", _PATH, exc)


def _describe_gap(started: str, last: str) -> str:
    """How long the previous run lasted, in words, for the log line."""
    try:
        a = datetime.fromisoformat(started)
        b = datetime.fromisoformat(last)
        mins = (b - a).total_seconds() / 60
        if mins < 60:
            return f"{mins:.0f} min"
        if mins < 60 * 48:
            return f"{mins / 60:.1f} h"
        return f"{mins / 1440:.1f} days"
    except Exception:
        return "unknown"


# How long after a planned-stop marker a death still counts as planned. The
# heartbeat ticks every 30s, and stopping a service takes seconds — so if the
# process was still beating minutes later, the marker was stale and whatever
# killed it afterwards was NOT the planned stop.
_PLANNED_GRACE_S = 180


def mark_planned_stop(reason: str = "planned restart") -> None:
    """Say a stop is about to happen, so the next start does not cry wolf.

    Task Scheduler and `systemctl stop` TERMINATE the process; no handler runs,
    so SUNI cannot record its own planned shutdown from the inside. Without this
    every deliberate restart logged "ended WITHOUT a clean shutdown" — accurate
    in the letter, and corrosive: a warning that fires on routine operations is
    one people learn to skip, which is exactly the failure the unclean-stop
    warning exists to prevent.

    Status stays "running" on purpose. If the stop then does not happen, nothing
    has been falsified — the marker simply ages out of the grace window and the
    next real crash is reported as a crash.
    """
    state = _read()
    if not state:
        return
    state["planned_stop_at"] = _now()
    state["planned_stop_reason"] = str(reason)[:200]
    _write(state)
    _log.info("[RUNSTATE] planned stop noted (%s)", reason)


def _was_planned(prev: dict) -> bool:
    """Did a planned-stop marker actually precede this death?"""
    marker = prev.get("planned_stop_at")
    last   = prev.get("last_heartbeat")
    if not marker or not last:
        return False
    try:
        gap = (datetime.fromisoformat(last)
               - datetime.fromisoformat(marker)).total_seconds()
    except Exception:
        return False
    return gap <= _PLANNED_GRACE_S


def previous_run() -> dict:
    """What the last run left behind, before this one overwrites it.

    Returns {"unclean": bool, "last_seen": str, "started_at": str, "pid": int,
    "ran_for": str, "reason": str}. An empty/missing file is a first run, which
    is NOT unclean — reporting a phantom crash on a fresh install would train
    the operator to ignore the warning that matters.
    """
    prev = _read()
    if not prev:
        return {"unclean": False, "first_run": True}
    status  = prev.get("status", "")
    planned = _was_planned(prev)
    return {
        "unclean":    status == "running" and not planned,
        "planned":    planned,
        "first_run":  False,
        "last_seen":  prev.get("last_heartbeat", ""),
        "started_at": prev.get("started_at", ""),
        "pid":        prev.get("pid", 0),
        "reason":     prev.get("reason", ""),
        "listening":  prev.get("listening"),
        "planned_reason": prev.get("planned_stop_reason", ""),
        "ran_for":    _describe_gap(prev.get("started_at", ""),
                                    prev.get("last_heartbeat", "")),
    }


def mark_started() -> dict:
    """Report on the previous run, then claim the file for this one.

    Call once at startup, BEFORE anything else can overwrite the file. Returns
    the same dict as previous_run() so the caller can surface it further (the
    admin panel, a notification) without reading the file twice.
    """
    prev = previous_run()
    if prev.get("unclean"):
        _log.warning(
            "[RUNSTATE] previous run (pid %s) ended WITHOUT a clean shutdown — "
            "last alive %s after %s. Nothing logged a stop, so it was killed "
            "rather than stopped: check the host's service manager.",
            prev.get("pid") or "?", prev.get("last_seen") or "unknown",
            prev.get("ran_for"),
        )
    elif prev.get("planned"):
        _log.info("[RUNSTATE] previous run (pid %s) was stopped deliberately "
                  "after %s (%s)", prev.get("pid") or "?", prev.get("ran_for"),
                  prev.get("planned_reason") or "planned restart")
    elif not prev.get("first_run"):
        _log.info("[RUNSTATE] previous run stopped cleanly (%s)",
                  prev.get("reason") or "no reason recorded")

    now = _now()
    _write({
        "status":         "running",
        "pid":            os.getpid(),
        "started_at":     now,
        "last_heartbeat": now,
        "reason":         "",
        "listening":      None,      # unknown until the first probe
    })
    return prev


def heartbeat(listening: bool | None = None) -> None:
    """Say "still alive, now" — and, if known, whether we are still SERVING.

    `listening` is the distinction that matters. A process can keep this
    heartbeat perfectly current while answering nobody, because its accept loop
    died and everything else kept running; that state was reproduced on
    01/09/2026 and every other signal SUNI had called it healthy. Recording it
    here means "alive but deaf" is answerable from the black box afterwards,
    rather than only by probing the port while it is still happening.

    Rewrites the whole file rather than patching it: it is a few hundred bytes,
    and a partial update is how a black box ends up describing a state that
    never existed.
    """
    state = _read()
    if not state:
        return                                    # mark_started() never ran
    state["last_heartbeat"] = _now()
    if listening is not None:
        state["listening"] = bool(listening)
    _write(state)


def mark_stopped(reason: str = "shutdown handler") -> None:
    """Record a clean stop. Only reached when the process gets to run code —
    which is precisely why its ABSENCE is the signal."""
    state = _read()
    if not state:
        state = {"pid": os.getpid(), "started_at": _now()}
    state["status"] = "stopped"
    state["reason"] = str(reason)[:200]
    state["stopped_at"] = _now()
    _write(state)
