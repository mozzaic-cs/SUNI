"""
Scheduled invocations — replay a prompt through SUNI on a cadence.

The existing scheduler tool creates Windows Task Scheduler entries that run OS
commands. This is a different thing: an entry here replays a PROMPT through the
orchestrator, optionally as a named agent, and delivers the answer. That is what
"summarise my calendar every morning" actually needs, and it is the primitive
agents were missing — without it an agent can only be invoked by a human sitting
at the UI.

Three rules, all of which exist because a scheduled run is UNATTENDED:

1. It runs as the owner, with the owner's role resolved AT FIRE TIME, never a
   snapshot taken when the schedule was created. Roles get downgraded; a
   schedule that kept yesterday's permissions would be a way to keep them.

2. The delivery destination is fixed by the human who created the schedule and
   is never chosen by the model. A scheduled run has no approver, so letting the
   model call send_email unattended would mean either denying it (useless) or
   allowing a model to pick recipients with nobody watching (worse). The runner
   sends; the model only writes.

3. An unrecognised cadence is an error, never a default. The tool this replaces
   silently turned "hourly" into "daily", which is the failure that looks like
   success.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

SCHEDULES_DB = "memory/schedules.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    owner_id     TEXT NOT NULL,
    owner_name   TEXT NOT NULL DEFAULT '',
    prompt       TEXT NOT NULL,
    agent_slug   TEXT NOT NULL DEFAULT '',   -- '' = SUNI answers as itself
    cadence      TEXT NOT NULL,              -- see parse_cadence()
    delivery     TEXT NOT NULL DEFAULT '{}', -- {"type":"email","to":"..."} | {}
    enabled      INTEGER NOT NULL DEFAULT 1,
    next_run     TEXT NOT NULL,
    last_run     TEXT NOT NULL DEFAULT '',
    last_status  TEXT NOT NULL DEFAULT '',
    run_count    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sched_due   ON schedules(enabled, next_run);
CREATE INDEX IF NOT EXISTS idx_sched_owner ON schedules(owner_id);
"""


def _conn() -> sqlite3.Connection:
    import os
    os.makedirs(os.path.dirname(SCHEDULES_DB) or ".", exist_ok=True)
    c = sqlite3.connect(SCHEDULES_DB)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CadenceError(ValueError):
    """Raised for a cadence that cannot be honoured, rather than substituting one."""


_DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_cadence(cadence: str) -> dict[str, Any]:
    """Understand a cadence, or refuse it.

    Accepted:
      every 15m / every 2h        — fixed interval
      hourly                      — at the top of every hour
      daily at 08:00              — once a day, local-to-UTC as given
      weekly on mon at 09:30      — once a week

    Anything else raises. The predecessor silently mapped unknown input to DAILY,
    so "every hour" became "every day" and looked like it had worked.
    """
    s = (cadence or "").strip().lower()

    m = re.fullmatch(r"every\s+(\d+)\s*(m|min|mins|minutes|h|hr|hrs|hours)", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        minutes = n * (60 if unit.startswith("h") else 1)
        if minutes < 1:
            raise CadenceError("interval must be at least one minute")
        return {"kind": "interval", "minutes": minutes}

    if s == "hourly":
        return {"kind": "hourly"}

    m = re.fullmatch(r"daily\s+at\s+(\d{1,2}):(\d{2})", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if not (0 <= h < 24 and 0 <= mi < 60):
            raise CadenceError(f"invalid time of day in {cadence!r}")
        return {"kind": "daily", "hour": h, "minute": mi}

    m = re.fullmatch(r"weekly\s+on\s+([a-z]{3})\s+at\s+(\d{1,2}):(\d{2})", s)
    if m:
        dow = m.group(1)
        if dow not in _DOW:
            raise CadenceError(f"unknown day {dow!r}")
        h, mi = int(m.group(2)), int(m.group(3))
        if not (0 <= h < 24 and 0 <= mi < 60):
            raise CadenceError(f"invalid time of day in {cadence!r}")
        return {"kind": "weekly", "dow": _DOW.index(dow), "hour": h, "minute": mi}

    raise CadenceError(
        f"cannot schedule {cadence!r}. Use: 'every 30m', 'every 2h', 'hourly', "
        f"'daily at 08:00', or 'weekly on mon at 09:30'."
    )


def next_after(cadence: str, after: datetime | None = None) -> datetime:
    """The next fire time strictly after `after`."""
    spec = parse_cadence(cadence)
    now = after or _now()
    k = spec["kind"]

    if k == "interval":
        return now + timedelta(minutes=spec["minutes"])
    if k == "hourly":
        return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    if k == "daily":
        cand = now.replace(hour=spec["hour"], minute=spec["minute"],
                           second=0, microsecond=0)
        return cand if cand > now else cand + timedelta(days=1)
    if k == "weekly":
        cand = now.replace(hour=spec["hour"], minute=spec["minute"],
                           second=0, microsecond=0)
        delta = (spec["dow"] - cand.weekday()) % 7
        cand += timedelta(days=delta)
        return cand if cand > now else cand + timedelta(days=7)
    raise CadenceError(f"unhandled cadence kind {k!r}")   # unreachable by design


# ── CRUD ─────────────────────────────────────────────────────────────────────
def create(name: str, prompt: str, cadence: str, owner_id: str,
           owner_name: str = "", agent_slug: str = "",
           delivery: dict | None = None) -> dict[str, Any]:
    nxt = next_after(cadence)          # raises before anything is written
    rec = {
        "id": uuid.uuid4().hex[:12], "name": name, "owner_id": owner_id,
        "owner_name": owner_name, "prompt": prompt, "agent_slug": agent_slug,
        "cadence": cadence, "delivery": json.dumps(delivery or {}),
        "enabled": 1, "next_run": nxt.isoformat(), "last_run": "",
        "last_status": "", "run_count": 0, "created_at": _now().isoformat(),
    }
    with _conn() as c:
        c.execute(
            """INSERT INTO schedules (id,name,owner_id,owner_name,prompt,agent_slug,
               cadence,delivery,enabled,next_run,last_run,last_status,run_count,created_at)
               VALUES (:id,:name,:owner_id,:owner_name,:prompt,:agent_slug,:cadence,
                       :delivery,:enabled,:next_run,:last_run,:last_status,:run_count,:created_at)""",
            rec)
    rec["delivery"] = delivery or {}
    return rec


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    try:
        d["delivery"] = json.loads(d.get("delivery") or "{}")
    except Exception:
        d["delivery"] = {}
    d["enabled"] = bool(d["enabled"])
    return d


def list_for_user(user_id: str, user_role: str = "") -> list[dict[str, Any]]:
    with _conn() as c:
        if user_role == "admin":
            rows = c.execute("SELECT * FROM schedules ORDER BY next_run").fetchall()
        else:
            rows = c.execute("SELECT * FROM schedules WHERE owner_id=? ORDER BY next_run",
                             (user_id,)).fetchall()
    return [_row(r) for r in rows]


def get(sched_id: str) -> dict[str, Any] | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM schedules WHERE id=?", (sched_id,)).fetchone()
    return _row(r) if r else None


def delete(sched_id: str, user_id: str, user_role: str = "") -> bool:
    with _conn() as c:
        r = c.execute("SELECT owner_id FROM schedules WHERE id=?", (sched_id,)).fetchone()
        if not r:
            return False
        if user_role != "admin" and r["owner_id"] != user_id:
            return False
        c.execute("DELETE FROM schedules WHERE id=?", (sched_id,))
    return True


def set_enabled(sched_id: str, enabled: bool, user_id: str, user_role: str = "") -> bool:
    with _conn() as c:
        r = c.execute("SELECT owner_id FROM schedules WHERE id=?", (sched_id,)).fetchone()
        if not r or (user_role != "admin" and r["owner_id"] != user_id):
            return False
        c.execute("UPDATE schedules SET enabled=? WHERE id=?", (1 if enabled else 0, sched_id))
    return True


# ── runner support ───────────────────────────────────────────────────────────
def due(now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or _now()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM schedules WHERE enabled=1 AND next_run<=? ORDER BY next_run",
            (now.isoformat(),)).fetchall()
    return [_row(r) for r in rows]


def mark_ran(sched_id: str, status: str, cadence: str) -> None:
    """Advance the clock whatever happened.

    Computed from NOW rather than from the missed slot: a machine that was asleep
    for a week should not wake up and fire seven times.
    """
    try:
        nxt = next_after(cadence).isoformat()
    except CadenceError:
        nxt = (_now() + timedelta(days=1)).isoformat()
    with _conn() as c:
        c.execute(
            """UPDATE schedules SET last_run=?, last_status=?, next_run=?,
               run_count=run_count+1 WHERE id=?""",
            (_now().isoformat(), status[:200], nxt, sched_id))
