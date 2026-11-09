"""The incident book: one row per distinct problem, however often it happens.

An incident is keyed by a fingerprint that survives the details changing — the
same exception at the same place in the code, the same diagnostics check, the
same request SUNI could not serve — so a fault that fires two hundred times is
one row with a count of two hundred, not two hundred rows nobody reads.

States:
  watching   a diagnostics finding seen once. Findings describe a state, and a
             state seen once may be a turn still running at the moment of the
             scan; it becomes an incident when the next scan sees it too.
  open       needs a look.
  resolved   it stopped: the finding cleared, or the error has not recurred
             for RESOLVE_AFTER_DAYS. Seen again, it opens again.
  dismissed  an admin said it does not matter. Still counted, never re-opened.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

UPKEEP_DB = "memory/upkeep.db"

RESOLVE_AFTER_DAYS = 7
EVIDENCE_MAX = 1500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id          TEXT PRIMARY KEY,           -- the fingerprint
    kind        TEXT NOT NULL,              -- finding | error
    code        TEXT NOT NULL DEFAULT '',   -- diagnostics code, exception type, log tag
    level       TEXT NOT NULL DEFAULT '',   -- broken | slow | error
    title       TEXT NOT NULL,
    location    TEXT NOT NULL DEFAULT '',   -- suni/x.py:func, or the library frame
    in_suni     INTEGER NOT NULL DEFAULT 0, -- the fault is in SUNI's own code
    evidence    TEXT NOT NULL DEFAULT '',   -- the latest sample, scrubbed
    source      TEXT NOT NULL DEFAULT '',   -- which log, or "diagnostics"
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    state       TEXT NOT NULL DEFAULT 'open',
    state_note  TEXT NOT NULL DEFAULT '',
    state_by    TEXT NOT NULL DEFAULT '',
    state_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_incidents_state ON incidents(state, last_seen);
-- What SUNI told someone she could not do. Kept apart from incidents because
-- it holds a person's words: it is keyed by user, listed in erasure.py, and
-- only ever a list for an admin to read — a request is not a fault to fix.
CREATE TABLE IF NOT EXISTS gaps (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL DEFAULT '',
    request     TEXT NOT NULL,              -- the start of what was asked, scrubbed
    reply       TEXT NOT NULL DEFAULT '',   -- the start of her answer, scrubbed
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    state       TEXT NOT NULL DEFAULT 'open'  -- open | dismissed
);
CREATE TABLE IF NOT EXISTS cursors (
    path        TEXT PRIMARY KEY,
    offset      INTEGER NOT NULL,
    head        TEXT NOT NULL DEFAULT ''    -- hash of the first bytes: a new file
);
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(UPKEEP_DB) or ".", exist_ok=True)
    c = sqlite3.connect(UPKEEP_DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


def _audit(action: str, detail: str, target: str, by_id: str = "", by: str = "") -> None:
    """by_id is the admin's user id: erasure finds a person's audit rows by it."""
    try:
        from .. import audit
        audit.log_event(by_id or "system", by or "upkeep", action, detail, target)
    except Exception:      # noqa: BLE001 — the book must not fail on the audit
        pass


def record(*, id: str, kind: str, title: str, code: str = "", level: str = "",
           location: str = "", in_suni: bool = False, evidence: str = "",
           source: str = "", seen_at: str = "", count: int = 1,
           initial_state: str = "open", history: bool = False) -> str:
    """Count one sighting (or `count` of them). Returns the state it is in now.

    `history` is a sighting from before upkeep was watching: it is counted,
    but it neither opens an incident nor re-opens a resolved one."""
    at = seen_at or now()
    evidence = (evidence or "")[:EVIDENCE_MAX]
    if history:
        initial_state = "resolved"
    with _conn() as c:
        row = c.execute("SELECT state, last_seen FROM incidents WHERE id=?", (id,)).fetchone()
        if row is None:
            c.execute(
                """INSERT INTO incidents (id, kind, code, level, title, location, in_suni,
                       evidence, source, first_seen, last_seen, count, state, state_note,
                       state_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (id, kind, code, level, title[:300], location, int(in_suni), evidence,
                 source, at, at, count, initial_state,
                 "seen before upkeep started watching" if history else "", now()))
            if initial_state == "open":
                _audit("upkeep.incident.opened", title, id)
            return initial_state
        state = row["state"]
        new_state, note = state, None
        if state == "resolved" and not history:
            new_state, note = "open", "came back after it was resolved"
        c.execute(
            """UPDATE incidents SET count = count + ?, last_seen = MAX(last_seen, ?),
                   evidence = ?, level = ?, title = ? WHERE id = ?""",
            (count, at, evidence, level, title[:300], id))
        if new_state != state:
            c.execute("UPDATE incidents SET state=?, state_note=?, state_by='', state_at=? "
                      "WHERE id=?", (new_state, note, now(), id))
            _audit("upkeep.incident.reopened", title, id)
        return new_state


def set_state(id: str, state: str, note: str = "", by: str = "",
              by_id: str = "") -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM incidents WHERE id=?", (id,)).fetchone()
        if row is None:
            return None
        c.execute("UPDATE incidents SET state=?, state_note=?, state_by=?, state_at=? "
                  "WHERE id=?", (state, note, by, now(), id))
    _audit(f"upkeep.incident.{state}", row["title"], id, by_id, by)
    return get(id)


def get(id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM incidents WHERE id=?", (id,)).fetchone()
    return _row(row) if row else None


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["in_suni"] = bool(d["in_suni"])
    return d


_STATE_ORDER = "CASE state WHEN 'open' THEN 0 WHEN 'watching' THEN 1 " \
               "WHEN 'dismissed' THEN 2 ELSE 3 END"


def list_incidents(kind: str = "", limit: int = 200) -> list[dict]:
    q = "SELECT * FROM incidents"
    args: list = []
    if kind:
        q += " WHERE kind = ?"
        args.append(kind)
    q += f" ORDER BY {_STATE_ORDER}, last_seen DESC LIMIT ?"
    args.append(int(limit))
    with _conn() as c:
        return [_row(r) for r in c.execute(q, args).fetchall()]


def counts() -> dict:
    with _conn() as c:
        rows = c.execute("SELECT kind, state, COUNT(*) n FROM incidents "
                         "GROUP BY kind, state").fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["kind"], {})[r["state"]] = r["n"]
    return out


def findings_seen(present: set[str]) -> None:
    """Settle the diagnostics findings after a scan: the ones not present any
    more are over. A finding seen once and gone was a moment, not a problem."""
    with _conn() as c:
        rows = c.execute("SELECT id, state, title FROM incidents WHERE kind='finding' "
                         "AND state IN ('watching','open')").fetchall()
        for r in rows:
            if r["id"] in present:
                continue
            if r["state"] == "watching":
                c.execute("DELETE FROM incidents WHERE id=?", (r["id"],))
            else:
                c.execute("UPDATE incidents SET state='resolved', "
                          "state_note='the check no longer reports it', state_by='', "
                          "state_at=? WHERE id=?", (now(), r["id"]))
                _audit("upkeep.incident.resolved", r["title"], r["id"])


def promote_watching(present: set[str]) -> None:
    """A finding present on two scans in a row is an incident."""
    with _conn() as c:
        for id in present:
            r = c.execute("SELECT state, title, count FROM incidents WHERE id=?",
                          (id,)).fetchone()
            if r and r["state"] == "watching" and r["count"] >= 2:
                c.execute("UPDATE incidents SET state='open', state_at=? WHERE id=?",
                          (now(), id))
                _audit("upkeep.incident.opened", r["title"], id)


def resolve_quiet_errors(days: int = RESOLVE_AFTER_DAYS) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with _conn() as c:
        rows = c.execute("SELECT id, title FROM incidents WHERE kind='error' "
                         "AND state='open' AND last_seen < ?", (cutoff,)).fetchall()
        for r in rows:
            c.execute("UPDATE incidents SET state='resolved', state_note=?, state_by='', "
                      "state_at=? WHERE id=?",
                      (f"not seen for {days} days", now(), r["id"]))
            _audit("upkeep.incident.resolved", r["title"], r["id"])
    return len(rows)


# ── what she could not do ────────────────────────────────────────────────────

def record_gap(user_id: str, key: str, request: str, reply: str) -> None:
    id = fingerprint("gap", user_id, key)
    at = now()
    with _conn() as c:
        c.execute(
            """INSERT INTO gaps (id, user_id, request, reply, first_seen, last_seen)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET count = count + 1, last_seen = excluded.last_seen,
                   request = excluded.request, reply = excluded.reply""",
            (id, user_id, request, reply, at, at))


def list_gaps(limit: int = 100) -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM gaps ORDER BY state = 'dismissed', last_seen DESC LIMIT ?",
            (int(limit),)).fetchall()]


def set_gap_state(id: str, state: str, by: str = "", by_id: str = "") -> bool:
    with _conn() as c:
        n = c.execute("UPDATE gaps SET state=? WHERE id=?", (state, id)).rowcount
    if n:
        _audit(f"upkeep.gap.{state}", "", id, by_id, by)
    return bool(n)


# ── where each log was read up to ────────────────────────────────────────────

def cursor(path: str) -> tuple[int, str] | None:
    with _conn() as c:
        r = c.execute("SELECT offset, head FROM cursors WHERE path=?", (path,)).fetchone()
    return (r["offset"], r["head"]) if r else None


def set_cursor(path: str, offset: int, head: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO cursors (path, offset, head) VALUES (?,?,?) "
                  "ON CONFLICT(path) DO UPDATE SET offset=excluded.offset, head=excluded.head",
                  (path, offset, head))


def set_meta(key: str, value) -> None:
    with _conn() as c:
        c.execute("INSERT INTO meta (key, value) VALUES (?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))


def get_meta(key: str, default=None):
    with _conn() as c:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except ValueError:
        return default
