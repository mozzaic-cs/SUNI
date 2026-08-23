"""
Everything SUNI holds about one person, as a file — GDPR Art 15 and Art 20.

The mirror of `erasure.py`, and it reads that module's inventory rather than
keeping its own. Two separate lists is how you get an export that omits a store
erasure deletes (the subject is told less than exists) or covers one it does not
(the subject is told their data was removed when it was not). Both are lies to a
data subject, so there is exactly one inventory and a test that fails when a
user-keyed table appears outside it.

**Columns are allow-listed, never deny-listed.** The export is a file that
leaves the machine, and a denylist fails open: a credential column added
upstream would be included by default and nobody would notice until it had
already been handed to someone. `EXPORT_COLUMNS` names what goes out; everything
else is dropped, so a new column is absent until someone adds it deliberately.
That is why `password_h`, `api_token_h`, `anthropic_api_key` and `smtp_pass`
cannot appear here — not because they are listed as forbidden, but because they
are not listed at all.

Other people are not in scope either. The subject's own membership of an agent
is theirs; the identities of the OTHER members of that agent are not, so
`agent_members` exports the row's `slug` and `role` and drops the user column.

What cannot be exported is stated, not omitted: the same
`unattributed_stores()` the erasure preview shows. A store that cannot be
filtered to one person cannot be handed to them either.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .erasure import (AUDIT_TABLE, JOIN_TABLES, ROOT, SUBJECT_TABLES,
                      _db, _personal_memory_dir, unattributed_stores)

# What leaves the machine, per table. Anything absent is dropped.
EXPORT_COLUMNS: dict[str, tuple[str, ...]] = {
    # No password_h, no api_token_h — they are credentials, not personal data
    # the subject needs, and an export is a file that gets emailed around.
    "users": ("id", "username", "role", "active", "created_at", "last_login",
              "auth_source"),
    "oidc_identities": ("issuer", "sub", "created_at"),
    "conversations": ("id", "title", "created_at", "updated_at"),
    "messages": ("id", "conversation_id", "role", "content", "ts"),
    "schedules": ("id", "name", "prompt", "agent_slug", "cadence", "delivery",
                  "enabled", "next_run", "last_run", "last_status", "run_count",
                  "created_at"),
    "agents": ("slug", "name", "description", "model", "mode", "tools_json",
               "blocked_json", "mcp_json", "enabled", "created_at", "updated_at",
               "used_count", "last_used", "max_steps", "max_runs_day"),
    # slug + role only: the co-members of a shared agent are other people.
    "agent_members": ("slug", "role", "added_at"),
    "bg_tasks": ("id", "title", "description", "status", "created_at",
                 "started_at", "completed_at", "result", "error", "progress",
                 "notify_channel"),
    "watch_items": ("id", "type", "value", "enabled", "created_at"),
    "projects": ("id", "name", "goal", "status", "context_json", "action_log",
                 "created_at", "updated_at"),
    # project_id + role only — same rule as agent_members: the other people on
    # a shared project are not this subject's data.
    "project_members": ("project_id", "role", "added_at"),
    # The subject's own audit rows, including their query previews and IPs —
    # that IS their data, and Art 15 entitles them to it.
    "audit_log": ("ts", "session_id", "ip_address", "query_preview", "route",
                  "mode", "tools_called", "tool_errors", "duration_s",
                  "approved_by", "prompt_tokens", "gen_tokens", "agent_slug"),
}

# Per-user settings worth returning. The encrypted fields
# (anthropic_api_key, smtp_pass) are deliberately absent.
SETTINGS_KEYS: tuple[str, ...] = (
    "stt_language", "response_language", "tts_voice", "allowed_mcp_servers",
    "output_dir", "smtp_host", "smtp_port", "smtp_user", "notify_to",
    "imap_host", "imap_port",
)


def _rows(path: Path, table: str, where: str, args: tuple) -> list[dict[str, Any]]:
    """Select the allow-listed columns that this table actually has."""
    if not path.exists():
        return []
    allowed = EXPORT_COLUMNS.get(table)
    if not allowed:
        return []
    try:
        with sqlite3.connect(str(path)) as c:
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            cols = [x for x in allowed if x in have]
            if not cols:
                return []
            c.row_factory = sqlite3.Row
            sel = ", ".join(cols)
            return [dict(r) for r in
                    c.execute(f"SELECT {sel} FROM {table} WHERE {where}", args)]
    except sqlite3.Error:
        return []


def _personal_memory(root: Path, user_id: str) -> list[dict[str, Any]]:
    """The subject's own memory entries, without their embedding vectors.

    The `e16` blob is a float16 embedding — machine state, not something a
    person can read or reuse, and it roughly doubles the file size.
    """
    p = _personal_memory_dir(root, user_id) / "suni_memory.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data if isinstance(data, list) else data.get("entries", [])
    if not isinstance(entries, list):
        return []
    return [{k: v for k, v in e.items() if k != "e16"}
            for e in entries if isinstance(e, dict)]


def _settings(root: Path, user_id: str) -> dict[str, Any]:
    p = _personal_memory_dir(root, user_id) / "settings.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in SETTINGS_KEYS if k in data}


def export(user_id: str, root: Path | None = None) -> dict[str, Any]:
    """Everything held about `user_id`, as a JSON-serialisable dict.

    Read-only. Safe to call as often as anyone likes.
    """
    root = root or ROOT
    data: dict[str, Any] = {}

    for db_name, table, column, label in SUBJECT_TABLES:
        data[label] = _rows(_db(root, db_name), table, f"{column}=?", (user_id,))
    for db_name, table, fk, parent, pkey, pcol, label in JOIN_TABLES:
        data[label] = _rows(
            _db(root, db_name), table,
            f"{fk} IN (SELECT {pkey} FROM {parent} WHERE {pcol}=?)", (user_id,))

    audit_db, audit_table, audit_col = AUDIT_TABLE
    data["audit_log"] = _rows(_db(root, audit_db), audit_table,
                              f"{audit_col}=?", (user_id,))
    data["personal_memory"] = _personal_memory(root, user_id)
    data["settings"] = _settings(root, user_id)

    account = data.get("account") or [{}]
    return {
        "subject": {
            "user_id": user_id,
            "username": (account[0] or {}).get("username", ""),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "format": "SUNI subject access export v1",
        "data": data,
        "counts": {k: len(v) if isinstance(v, list) else 1
                   for k, v in data.items() if v},
        "not_included": unattributed_stores(root),
        "notes": [
            "Credentials are deliberately excluded: password hashes, API tokens "
            "and the encrypted per-user API key and mail password are not "
            "personal data the subject needs, and this file travels.",
            "Embedding vectors are omitted from memory entries — they are "
            "machine state, not readable content.",
            "Where an agent is shared, only this subject's own membership is "
            "included; the other members are other people.",
        ],
    }


def to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)
