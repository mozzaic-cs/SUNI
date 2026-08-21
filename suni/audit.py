"""
Audit trail — append-only SQLite log of every user interaction.

Database: memory/audit.db
Retention: `audit_retention_days` in config. **Default 0 = keep everything.**
    apply_retention() enforces it once a day from the scheduler loop. This
    docstring previously claimed a 90-day auto-purge; nothing called purge_old()
    at all, so the claim was false in the direction that matters — an operator
    reading it would believe old records were being cleaned up when they were
    accumulating forever.
Privacy: only query previews (100 chars), not full conversation content.
"""
from __future__ import annotations
import csv
import io
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DB = Path("memory/audit.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB))
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    _DB.parent.mkdir(exist_ok=True)
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              TEXT NOT NULL,
                user_id         TEXT NOT NULL,
                username        TEXT NOT NULL,
                session_id      TEXT,
                ip_address      TEXT,
                query_preview   TEXT,
                route           TEXT,
                mode            TEXT DEFAULT 'assistant',
                tools_called    TEXT,
                tool_errors     INTEGER DEFAULT 0,
                duration_s      REAL,
                approved_by     TEXT
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ts ON audit_log(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_uid ON audit_log(user_id)")
        # Per-request token accounting (SMB cost visibility). Added as columns on
        # the existing per-request row so they surface in the Audit tab + CSV.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(audit_log)").fetchall()}
        if "prompt_tokens" not in cols:
            c.execute("ALTER TABLE audit_log ADD COLUMN prompt_tokens INTEGER DEFAULT 0")
        if "gen_tokens" not in cols:
            c.execute("ALTER TABLE audit_log ADD COLUMN gen_tokens INTEGER DEFAULT 0")
        # Which user-defined agent profile handled the request, if any. Empty
        # means SUNI answered as itself. Without this a request run under a
        # profile — different prompt, possibly a different model, a narrowed
        # tool set — is indistinguishable in the log from an ordinary one.
        if "agent_slug" not in cols:
            c.execute("ALTER TABLE audit_log ADD COLUMN agent_slug TEXT DEFAULT ''")


def log(
    user_id:       str,
    username:      str,
    session_id:    str = "",
    ip_address:    str = "",
    query_preview: str = "",
    route:         str = "",
    mode:          str = "assistant",
    tools_called:  list[str] | None = None,
    tool_errors:   int = 0,
    duration_s:    float = 0.0,
    approved_by:   str = "",
    prompt_tokens: int = 0,
    gen_tokens:    int = 0,
    agent_slug:    str = "",
) -> None:
    tools_str = ",".join(tools_called) if tools_called else ""
    with _conn() as c:
        c.execute(
            """INSERT INTO audit_log
               (ts, user_id, username, session_id, ip_address, query_preview,
                route, mode, tools_called, tool_errors, duration_s, approved_by,
                prompt_tokens, gen_tokens, agent_slug)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                user_id, username, session_id, ip_address,
                query_preview[:100],
                route, mode, tools_str, tool_errors, duration_s, approved_by,
                int(prompt_tokens or 0), int(gen_tokens or 0), agent_slug,
            ),
        )


def log_event(
    user_id:   str,
    username:  str,
    action:    str,            # e.g. "memory.promote.approved"
    detail:    str = "",
    target_id: str = "",
    ip_address: str = "",
    agent_slug: str = "",
) -> None:
    """
    Log a non-chat governance/system event, reusing the audit_log table so it
    surfaces in the existing Audit tab and CSV export. Mapping: route=action,
    mode='governance', query_preview=detail, tools_called=target entry id.
    (A dedicated schema is a Phase 2/3 concern; this keeps Phase 1 zero-migration.)
    """
    with _conn() as c:
        c.execute(
            """INSERT INTO audit_log
               (ts, user_id, username, session_id, ip_address, query_preview,
                route, mode, tools_called, tool_errors, duration_s, approved_by,
                agent_slug)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                user_id, username, "", ip_address,
                detail[:100], action, "governance", target_id, 0, 0.0, user_id,
                agent_slug,
            ),
        )


def query(
    limit:     int = 50,
    offset:    int = 0,
    user_id:   str | None = None,
    date_from: str | None = None,   # ISO date string YYYY-MM-DD
    date_to:   str | None = None,
    route:     str | None = None,
    tool:      str | None = None,
) -> tuple[list[dict], int]:
    """Returns (rows, total_count)."""
    wheres, params = [], []

    if user_id:
        wheres.append("user_id=?"); params.append(user_id)
    if date_from:
        wheres.append("ts>=?"); params.append(date_from)
    if date_to:
        wheres.append("ts<=?"); params.append(date_to + "T23:59:59")
    if route:
        wheres.append("route=?"); params.append(route)
    if tool:
        wheres.append("tools_called LIKE ?"); params.append(f"%{tool}%")

    where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""

    with _conn() as c:
        total = c.execute(f"SELECT COUNT(*) FROM audit_log {where_sql}",
                          params).fetchone()[0]
        rows  = c.execute(
            f"SELECT * FROM audit_log {where_sql} ORDER BY ts DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

    return [dict(r) for r in rows], total


def export_csv(
    user_id:   str | None = None,
    date_from: str | None = None,
    date_to:   str | None = None,
) -> str:
    rows, _ = query(limit=100_000, user_id=user_id,
                    date_from=date_from, date_to=date_to)
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    return buf.getvalue()


def purge_old(days: int) -> int:
    """Delete audit rows older than `days`. Refuses a non-positive `days`.

    The guard is the point. `purge_old(0)` computes a cutoff of "now" and
    deletes the entire audit trail — and 0 is exactly the value the config uses
    to mean "keep everything", so an unguarded call would do the precise
    opposite of what the setting says. Same for a negative, which would delete
    rows from the future and then, on the next tick, everything.
    """
    if days <= 0:
        raise ValueError(
            "purge_old() needs a positive number of days; "
            f"got {days!r}. 0 means 'keep everything' — do not call this.")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as c:
        n = c.execute("DELETE FROM audit_log WHERE ts<?", (cutoff,)).rowcount
    return n


# The retention pass runs at most once a day; the caller is a 30-second loop.
_last_retention_pass: str = ""


def apply_retention(force: bool = False) -> dict:
    """Enforce `audit_retention_days`, at most once per calendar day.

    Returns what it did, so the caller can log it. Never raises: a failure here
    must not take down the scheduler that calls it, and losing a retention pass
    is recoverable while losing the loop is not.
    """
    global _last_retention_pass
    out = {"ran": False, "deleted": 0, "days": 0, "reason": ""}
    try:
        from . import config
        days = int(config.load().get("audit_retention_days", 0) or 0)
    except Exception as exc:                     # noqa: BLE001
        out["reason"] = f"could not read config: {exc}"
        return out
    out["days"] = days
    if days <= 0:
        out["reason"] = "retention disabled (keeping everything)"
        return out

    today = datetime.now(timezone.utc).date().isoformat()
    if not force and _last_retention_pass == today:
        out["reason"] = "already ran today"
        return out
    try:
        out["deleted"] = purge_old(days)
        _last_retention_pass = today
        out["ran"] = True
    except Exception as exc:                     # noqa: BLE001
        out["reason"] = f"purge failed: {exc}"
    return out


def usage_summary(days: int = 30) -> dict:
    """Per-user token usage over the last `days`, for the admin Usage view.
    Returns {"window_days", "totals": {...}, "by_user": [ {user_id, username,
    requests, prompt_tokens, gen_tokens, total_tokens}, ... ]}."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as c:
        rows = c.execute(
            """SELECT user_id, username,
                      COUNT(*)                AS requests,
                      COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
                      COALESCE(SUM(gen_tokens),0)    AS gen_tokens
               FROM audit_log
               WHERE ts>=? AND (prompt_tokens>0 OR gen_tokens>0)
               GROUP BY user_id
               ORDER BY (COALESCE(SUM(prompt_tokens),0)+COALESCE(SUM(gen_tokens),0)) DESC""",
            (cutoff,),
        ).fetchall()
    by_user, tp, tg, tr = [], 0, 0, 0
    for r in rows:
        p, g = r["prompt_tokens"], r["gen_tokens"]
        tp += p; tg += g; tr += r["requests"]
        by_user.append({
            "user_id": r["user_id"], "username": r["username"],
            "requests": r["requests"], "prompt_tokens": p, "gen_tokens": g,
            "total_tokens": p + g,
        })
    # Breakdown by conversation mode — makes the costly "collaborate" (Mode 2 /
    # frontier) usage visible as its own line. Note: subprocess CLI models
    # (Claude Code / Codex) don't surface token counts, so for collaborate the
    # REQUEST count is the cost proxy (each run ≈ several frontier calls).
    with _conn() as c:
        mrows = c.execute(
            """SELECT COALESCE(mode,'assistant') AS mode,
                      COUNT(*) AS requests,
                      COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
                      COALESCE(SUM(gen_tokens),0)    AS gen_tokens
               FROM audit_log
               WHERE ts>=? AND route='chat'
               GROUP BY COALESCE(mode,'assistant')
               ORDER BY requests DESC""",
            (cutoff,),
        ).fetchall()
    by_mode = [{"mode": r["mode"], "requests": r["requests"],
                "prompt_tokens": r["prompt_tokens"], "gen_tokens": r["gen_tokens"],
                "total_tokens": r["prompt_tokens"] + r["gen_tokens"]} for r in mrows]
    return {
        "window_days": days,
        "totals": {"requests": tr, "prompt_tokens": tp, "gen_tokens": tg,
                   "total_tokens": tp + tg},
        "by_user": by_user,
        "by_mode": by_mode,
    }


def stats() -> dict:
    with _conn() as c:
        total    = c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_n  = c.execute("SELECT COUNT(*) FROM audit_log WHERE ts>=?",
                             (today,)).fetchone()[0]
        users_n  = c.execute("SELECT COUNT(DISTINCT user_id) FROM audit_log"
                             ).fetchone()[0]
    return {"total": total, "today": today_n, "unique_users": users_n}
