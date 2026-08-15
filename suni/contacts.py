"""
CRM-lite contact store for SUNI.

Stores contacts in memory/contacts.db with full-text search across
name, email, company and timestamped interaction notes.
"""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

_DB = Path("memory/contacts.db")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS contacts (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    company    TEXT NOT NULL DEFAULT '',
    email      TEXT NOT NULL DEFAULT '',
    phone      TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    tags       TEXT NOT NULL DEFAULT '[]',
    last_seen  TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_name    ON contacts(lower(name));
CREATE INDEX IF NOT EXISTS idx_contacts_email   ON contacts(lower(email));
CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(lower(company));
CREATE INDEX IF NOT EXISTS idx_contacts_updated ON contacts(updated_at DESC);
"""


def _conn() -> sqlite3.Connection:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA_SQL)
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except (ValueError, TypeError):
        d["tags"] = []
    return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── CRUD ─────────────────────────────────────────────────────────────────────

def add_contact(
    name: str,
    company: str = "",
    email: str = "",
    phone: str = "",
    notes: str = "",
    tags: list[str] | None = None,
) -> dict:
    now = _now()
    cid = str(uuid.uuid4())[:8]
    with _conn() as conn:
        conn.execute(
            "INSERT INTO contacts "
            "(id, name, company, email, phone, notes, tags, last_seen, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, name.strip(), company, email, phone, notes,
             json.dumps(tags or []), now, now, now),
        )
    return get_contact(cid)


def get_contact(contact_id: str) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def update_contact(
    contact_id: str,
    name: str | None = None,
    company: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if not row:
        conn.close()
        return None
    d = _row_to_dict(row)
    updates = {k: v for k, v in dict(
        name=name, company=company, email=email,
        phone=phone, notes=notes, tags=tags,
    ).items() if v is not None}
    d.update(updates)
    now = _now()
    with conn:
        conn.execute(
            "UPDATE contacts SET name=?, company=?, email=?, phone=?, notes=?, tags=?, updated_at=? "
            "WHERE id=?",
            (d["name"], d["company"], d["email"], d["phone"], d["notes"],
             json.dumps(d["tags"]) if isinstance(d["tags"], list) else d["tags"],
             now, contact_id),
        )
    conn.close()
    return get_contact(contact_id)


def append_note(contact_id: str, note: str) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT notes FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if not row:
        conn.close()
        return None
    now = _now()
    ts_note   = f"[{now[:10]}] {note.strip()}"
    existing  = row["notes"] or ""
    new_notes = f"{existing}\n{ts_note}".strip() if existing else ts_note
    with conn:
        conn.execute(
            "UPDATE contacts SET notes=?, last_seen=?, updated_at=? WHERE id=?",
            (new_notes, now, now, contact_id),
        )
    conn.close()
    return get_contact(contact_id)


def search_contacts(query: str, limit: int = 20) -> list[dict]:
    q = f"%{query.lower()}%"
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM contacts "
        "WHERE lower(name) LIKE ? OR lower(email) LIKE ? OR lower(company) LIKE ? OR lower(notes) LIKE ? "
        "ORDER BY updated_at DESC LIMIT ?",
        (q, q, q, q, limit),
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def list_contacts(limit: int = 50, offset: int = 0) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM contacts ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    conn.close()
    return [_row_to_dict(r) for r in rows], count


def delete_contact(contact_id: str) -> bool:
    conn = _conn()
    cur = conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0
