"""
Persistent projects for SUNI.

A project is a named, resumable work context that survives across sessions.
It stores goals, linked files, contacts, and an action log.
Projects can be shared with multiple users at different access levels.

Storage: memory/projects.db (SQLite, WAL mode)
"""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

_DB = Path("memory/projects.db")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    goal         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active',  -- active | paused | done
    user_id      TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL DEFAULT '{}',       -- {files:[], contacts:[], notes:""}
    action_log   TEXT NOT NULL DEFAULT '[]',       -- [{ts, action, by}]
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_members (
    project_id TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'editor',   -- owner | editor | viewer
    added_at   TEXT NOT NULL,
    PRIMARY KEY (project_id, user_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_proj_user      ON projects(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_proj_status    ON projects(status);
CREATE INDEX IF NOT EXISTS idx_members_user   ON project_members(user_id);
CREATE INDEX IF NOT EXISTS idx_members_proj   ON project_members(project_id);
"""

_ROLE_RANK: dict[str, int] = {"viewer": 0, "editor": 1, "owner": 2, "admin": 99}


def _conn() -> sqlite3.Connection:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA_SQL)
    _migrate_members(conn)
    return conn


def _migrate_members(conn: sqlite3.Connection) -> None:
    """Backfill project_members for projects created before membership was added."""
    rows = conn.execute(
        "SELECT id, user_id FROM projects WHERE user_id != ''"
    ).fetchall()
    now = _now()
    for row in rows:
        conn.execute(
            "INSERT OR IGNORE INTO project_members (project_id, user_id, role, added_at) "
            "VALUES (?, ?, 'owner', ?)",
            (row["id"], row["user_id"], now),
        )
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("context_json", "action_log"):
        try:
            d[key] = json.loads(d.get(key) or ("{}") if key == "context_json" else "[]")
        except (ValueError, TypeError):
            d[key] = {} if key == "context_json" else []
    return d


# ── Access control ────────────────────────────────────────────────────────────

def can_access(
    project_id: str,
    user_id: str,
    is_admin: bool = False,
    min_role: str = "viewer",
) -> str | None:
    """
    Return the user's effective role if they meet min_role, else None.

    is_admin=True grants 'admin' override for non-owner operations.
    Delete and share (owner-only) pass min_role='owner', which admin cannot override.
    """
    conn = _conn()
    row = conn.execute(
        "SELECT role FROM project_members WHERE project_id=? AND user_id=?",
        (project_id, user_id),
    ).fetchone()
    conn.close()
    if row:
        role = row["role"]
        if _ROLE_RANK.get(role, -1) >= _ROLE_RANK.get(min_role, 0):
            return role
        return None  # member but insufficient role
    if is_admin and min_role != "owner":
        return "admin"  # admin override, but not for owner-only operations
    return None


# ── CRUD ─────────────────────────────────────────────────────────────────────

def create_project(name: str, goal: str = "", user_id: str = "") -> dict:
    pid = str(uuid.uuid4())[:10]
    now = _now()
    ctx = json.dumps({"files": [], "contacts": [], "notes": ""})
    log = json.dumps([])
    with _conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, goal, status, user_id, context_json, action_log, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            (pid, name.strip(), goal, user_id, ctx, log, now, now),
        )
        if user_id:
            conn.execute(
                "INSERT OR IGNORE INTO project_members (project_id, user_id, role, added_at) "
                "VALUES (?, ?, 'owner', ?)",
                (pid, user_id, now),
            )
    return get_project(pid) or {}


def get_project(project_id: str, requesting_user_id: str = "") -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        conn.close()
        return None
    d = _row_to_dict(row)
    members = conn.execute(
        "SELECT user_id, role, added_at FROM project_members WHERE project_id = ? ORDER BY added_at",
        (project_id,),
    ).fetchall()
    conn.close()
    d["members"] = [dict(m) for m in members]
    if requesting_user_id:
        match = next((m for m in d["members"] if m["user_id"] == requesting_user_id), None)
        d["my_role"] = match["role"] if match else None
    return d


def list_projects(user_id: str = "", status: str = "") -> list[dict]:
    """List projects accessible to user_id (all where they are a member)."""
    conn = _conn()
    sql = """
        SELECT DISTINCT p.*, pm.role AS my_role
        FROM projects p
        JOIN project_members pm ON pm.project_id = p.id
        WHERE 1=1
    """
    args: list = []
    if user_id:
        sql += " AND pm.user_id = ?"; args.append(user_id)
    if status:
        sql += " AND p.status = ?"; args.append(status)
    sql += " ORDER BY p.updated_at DESC"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = _row_to_dict(r)
        result.append(d)
    return result


def update_project(
    project_id: str,
    name: str | None = None,
    goal: str | None = None,
    status: str | None = None,
) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        conn.close()
        return None
    d = _row_to_dict(row)
    if name    is not None: d["name"]   = name
    if goal    is not None: d["goal"]   = goal
    if status  is not None: d["status"] = status
    now = _now()
    with conn:
        conn.execute(
            "UPDATE projects SET name=?, goal=?, status=?, updated_at=? WHERE id=?",
            (d["name"], d["goal"], d["status"], now, project_id),
        )
    conn.close()
    return get_project(project_id)


def add_action(project_id: str, action: str, by: str = "user") -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT action_log FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        conn.close()
        return None
    try:
        log = json.loads(row["action_log"] or "[]")
    except (ValueError, TypeError):
        log = []
    log.append({"ts": _now()[:16], "action": action, "by": by})
    log = log[-100:]   # keep last 100 entries
    now = _now()
    with conn:
        conn.execute(
            "UPDATE projects SET action_log=?, updated_at=? WHERE id=?",
            (json.dumps(log), now, project_id),
        )
    conn.close()
    return get_project(project_id)


def link_file(project_id: str, path: str) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT context_json FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        conn.close()
        return None
    try:
        ctx = json.loads(row["context_json"] or "{}")
    except (ValueError, TypeError):
        ctx = {}
    files = ctx.get("files", [])
    if path not in files:
        files.append(path)
    ctx["files"] = files
    now = _now()
    with conn:
        conn.execute(
            "UPDATE projects SET context_json=?, updated_at=? WHERE id=?",
            (json.dumps(ctx), now, project_id),
        )
    conn.close()
    return get_project(project_id)


def link_contact(project_id: str, contact_id: str) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT context_json FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        conn.close()
        return None
    try:
        ctx = json.loads(row["context_json"] or "{}")
    except (ValueError, TypeError):
        ctx = {}
    contacts = ctx.get("contacts", [])
    if contact_id not in contacts:
        contacts.append(contact_id)
    ctx["contacts"] = contacts
    now = _now()
    with conn:
        conn.execute(
            "UPDATE projects SET context_json=?, updated_at=? WHERE id=?",
            (json.dumps(ctx), now, project_id),
        )
    conn.close()
    return get_project(project_id)


def set_notes(project_id: str, notes: str) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT context_json FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        conn.close()
        return None
    try:
        ctx = json.loads(row["context_json"] or "{}")
    except (ValueError, TypeError):
        ctx = {}
    ctx["notes"] = notes
    now = _now()
    with conn:
        conn.execute(
            "UPDATE projects SET context_json=?, updated_at=? WHERE id=?",
            (json.dumps(ctx), now, project_id),
        )
    conn.close()
    return get_project(project_id)


def build_context_block(project: dict) -> str:
    """Return a system-prompt-ready context string for a project."""
    ctx = project.get("context_json") or {}
    log = project.get("action_log") or []
    parts = [
        f"[ACTIVE PROJECT: {project['name']}]",
        f"Goal: {project['goal']}" if project.get("goal") else "",
        f"Status: {project['status']}",
    ]
    if ctx.get("notes"):
        parts.append(f"Notes:\n{ctx['notes'][:500]}")
    if ctx.get("files"):
        parts.append(f"Linked files: {', '.join(ctx['files'][:10])}")
    if ctx.get("contacts"):
        parts.append(f"Linked contacts: {', '.join(ctx['contacts'][:10])}")
    members = project.get("members", [])
    if len(members) > 1:
        member_strs = [f"{m['user_id']}({m['role']})" for m in members]
        parts.append(f"Members: {', '.join(member_strs[:10])}")
    if log:
        recent = log[-5:]
        parts.append("Recent actions:")
        for entry in recent:
            parts.append(f"  [{entry.get('ts','')}] {entry.get('action','')} (by {entry.get('by','')})")
    return "\n".join(p for p in parts if p)


def delete_project(project_id: str, user_id: str) -> bool:
    """Delete a project. Only the project owner can delete it."""
    conn = _conn()
    row = conn.execute(
        "SELECT role FROM project_members WHERE project_id=? AND user_id=?",
        (project_id, user_id),
    ).fetchone()
    if not row or row["role"] != "owner":
        conn.close()
        return False
    with conn:
        conn.execute("DELETE FROM project_members WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    conn.close()
    return True


# ── Membership management ─────────────────────────────────────────────────────

def get_members(project_id: str) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT user_id, role, added_at FROM project_members WHERE project_id=? ORDER BY added_at",
        (project_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_member(project_id: str, target_user_id: str, role: str = "editor") -> bool:
    """Add or update a member. Returns False if project not found or role invalid."""
    if role not in ("owner", "editor", "viewer"):
        return False
    now = _now()
    with _conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if not exists:
            return False
        conn.execute(
            "INSERT INTO project_members (project_id, user_id, role, added_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(project_id, user_id) DO UPDATE SET role=excluded.role",
            (project_id, target_user_id, role, now),
        )
    return True


def remove_member(project_id: str, target_user_id: str) -> bool:
    """Remove a member. Cannot remove the last owner."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT role FROM project_members WHERE project_id=? AND user_id=?",
            (project_id, target_user_id),
        ).fetchone()
        if not row:
            return False
        if row["role"] == "owner":
            owner_count = conn.execute(
                "SELECT COUNT(*) FROM project_members WHERE project_id=? AND role='owner'",
                (project_id,),
            ).fetchone()[0]
            if owner_count <= 1:
                return False  # cannot remove the last owner
        conn.execute(
            "DELETE FROM project_members WHERE project_id=? AND user_id=?",
            (project_id, target_user_id),
        )
    return True
