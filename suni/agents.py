"""
Agent profiles — named, reusable configurations of SUNI.

An agent is a saved answer to "who should handle this": a system prompt, an
optional model, and an optional narrowing of the tools and MCP servers it may
reach. Invoking one changes how a request is handled without changing anything
global, so several can coexist for different jobs.

Storage mirrors the skills system deliberately, so definitions stay readable and
editable outside the app:

  memory/agents/{slug}/AGENT.md   — YAML frontmatter + the system prompt as body
  memory/agents.db                — SQLite index, ownership and sharing

Because the definition is a file a user can edit, **nothing in it is trusted at
invocation time**. The grants it declares are re-resolved against the caller's
RBAC role on every run — see effective_grants(), which is the security core of
this module.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import rbac as _rbac

AGENTS_DIR = Path("memory/agents")
AGENTS_DB = Path("memory/agents.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    slug         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    owner_id     TEXT NOT NULL,
    model        TEXT NOT NULL DEFAULT '',
    mode         TEXT NOT NULL DEFAULT 'assistant',
    tools_json   TEXT NOT NULL DEFAULT 'null',   -- null = inherit the caller's
    blocked_json TEXT NOT NULL DEFAULT '[]',     -- always ADDED to the role's
    mcp_json     TEXT NOT NULL DEFAULT 'null',   -- null = inherit the caller's
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    used_count   INTEGER NOT NULL DEFAULT 0,
    last_used    TEXT NOT NULL DEFAULT ''
);
-- Sharing mirrors project_members so there is one sharing idiom in the codebase.
CREATE TABLE IF NOT EXISTS agent_members (
    slug     TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    role     TEXT NOT NULL DEFAULT 'viewer',   -- owner | editor | viewer
    added_at TEXT NOT NULL,
    PRIMARY KEY (slug, user_id),
    FOREIGN KEY (slug) REFERENCES agents(slug) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agents_owner   ON agents(owner_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_mem_user ON agent_members(user_id);
"""

_ROLE_RANK: dict[str, int] = {"viewer": 0, "editor": 1, "owner": 2, "admin": 99}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    AGENTS_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(AGENTS_DB)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or f"agent-{int(time.time())}"


# ─────────────────────────────────────────────────────────────────────────────
# The security core.
#
# An agent profile may only ever NARROW what the invoking user can already do.
# If it could widen, an admin-authored profile would become a privilege
# escalation for anyone allowed to invoke it — a restricted user picking
# "Ops Agent" from a dropdown and inheriting admin tool reach. So tool and MCP
# grants INTERSECT with the caller's role, and blocks UNION with it.
#
# This is re-derived on every invocation rather than stored, because AGENT.md is
# a file on disk that a user can edit without going through the app.
# ─────────────────────────────────────────────────────────────────────────────
def effective_grants(
    agent: dict[str, Any] | None,
    user_role: str,
    registered_prefixes: list[str] | None = None,
) -> dict[str, Any]:
    """Resolve what an invocation may actually reach.

    `None` for allowed_tools/mcp_prefixes carries the existing codebase meaning
    of "no restriction beyond the blocked list", so it is preserved rather than
    expanded into a concrete list — expanding it would silently freeze the set
    at whatever happened to be registered at that moment.
    """
    role_allowed = _rbac.allowed_tools(user_role)          # None = all
    role_blocked = list(_rbac.blocked_tools(user_role))
    role_prefixes = _rbac.mcp_prefixes(user_role, list(registered_prefixes or []))
    role_modes = _rbac.allowed_modes(user_role)

    if not agent:
        return {
            "allowed_tools": role_allowed,
            "blocked_tools": role_blocked,
            "mcp_prefixes": role_prefixes,
            "mode": role_modes[0] if role_modes else "assistant",
            "model": "",
            "system_prompt": "",
        }

    a_tools = agent.get("tools")            # None = inherit
    a_blocked = list(agent.get("blocked") or [])
    a_mcp = agent.get("mcp_servers")        # None = inherit

    # Tools: intersect. Either side being None means "that side adds no limit".
    if a_tools is None:
        allowed = role_allowed
    elif role_allowed is None:
        allowed = list(a_tools)
    else:
        allowed = [t for t in role_allowed if t in set(a_tools)]

    # Blocks accumulate — a block is a restriction, so more of them is safe.
    blocked = sorted(set(role_blocked) | set(a_blocked))

    # MCP prefixes: same intersection rule.
    if a_mcp is None:
        prefixes = role_prefixes
    elif role_prefixes is None:
        prefixes = list(a_mcp)
    else:
        prefixes = [p for p in role_prefixes if p in set(a_mcp)]

    # Mode: an agent cannot grant a mode the role is not entitled to.
    mode = str(agent.get("mode") or "assistant")
    if mode not in role_modes:
        mode = role_modes[0] if role_modes else "assistant"

    return {
        "allowed_tools": allowed,
        "blocked_tools": blocked,
        "mcp_prefixes": prefixes,
        "mode": mode,
        "model": str(agent.get("model") or ""),
        "system_prompt": str(agent.get("system_prompt") or ""),
    }


# ── AGENT.md read/write ──────────────────────────────────────────────────────
def _agent_md(slug: str) -> Path:
    return AGENTS_DIR / slug / "AGENT.md"


def _write_md(rec: dict[str, Any]) -> None:
    p = _agent_md(rec["slug"])
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = {
        "name": rec["name"],
        "slug": rec["slug"],
        "description": rec.get("description", ""),
        "model": rec.get("model", ""),
        "mode": rec.get("mode", "assistant"),
        "tools": rec.get("tools"),
        "blocked": rec.get("blocked") or [],
        "mcp_servers": rec.get("mcp_servers"),
        "enabled": bool(rec.get("enabled", True)),
        "created_at": rec.get("created_at", _now()),
        "updated_at": rec.get("updated_at", _now()),
    }
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {json.dumps(v)}" if not isinstance(v, str) else f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(rec.get("system_prompt", "").rstrip())
    lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")


def _read_md(slug: str) -> dict[str, Any]:
    """Frontmatter + body. The body IS the system prompt."""
    p = _agent_md(slug)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not m:
        return {"system_prompt": text.strip()}
    out: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip()
        try:
            out[k.strip()] = json.loads(v)
        except Exception:
            out[k.strip()] = v
    out["system_prompt"] = m.group(2).strip()
    return out


# ── CRUD ─────────────────────────────────────────────────────────────────────
def create(
    name: str,
    system_prompt: str,
    owner_id: str,
    description: str = "",
    model: str = "",
    mode: str = "assistant",
    tools: list[str] | None = None,
    blocked: list[str] | None = None,
    mcp_servers: list[str] | None = None,
) -> dict[str, Any]:
    slug = slugify(name)
    ts = _now()
    rec = {
        "slug": slug, "name": name, "description": description, "owner_id": owner_id,
        "model": model, "mode": mode, "tools": tools, "blocked": blocked or [],
        "mcp_servers": mcp_servers, "enabled": True, "created_at": ts, "updated_at": ts,
        "system_prompt": system_prompt,
    }
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO agents
               (slug,name,description,owner_id,model,mode,tools_json,blocked_json,
                mcp_json,enabled,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,1,?,?)""",
            (slug, name, description, owner_id, model, mode,
             json.dumps(tools), json.dumps(blocked or []), json.dumps(mcp_servers), ts, ts),
        )
        c.execute(
            "INSERT OR REPLACE INTO agent_members (slug,user_id,role,added_at) VALUES (?,?,'owner',?)",
            (slug, owner_id, ts),
        )
    _write_md(rec)
    return rec


def get(slug: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM agents WHERE slug=?", (slug,)).fetchone()
    if not row:
        return None
    rec = dict(row)
    rec["tools"] = json.loads(rec.pop("tools_json"))
    rec["blocked"] = json.loads(rec.pop("blocked_json"))
    rec["mcp_servers"] = json.loads(rec.pop("mcp_json"))
    rec["enabled"] = bool(rec["enabled"])
    # The file is the source of truth for the prompt: it is meant to be edited.
    rec["system_prompt"] = _read_md(slug).get("system_prompt", "")
    return rec


def list_for_user(user_id: str, user_role: str = "") -> list[dict[str, Any]]:
    """Agents the user owns or has been given access to. Admins see all."""
    with _conn() as c:
        if user_role == "admin":
            rows = c.execute("SELECT * FROM agents ORDER BY updated_at DESC").fetchall()
        else:
            rows = c.execute(
                """SELECT a.* FROM agents a
                   LEFT JOIN agent_members m ON m.slug = a.slug AND m.user_id = ?
                   WHERE a.owner_id = ? OR m.user_id IS NOT NULL
                   ORDER BY a.updated_at DESC""",
                (user_id, user_id),
            ).fetchall()
    out = []
    for r in rows:
        rec = dict(r)
        rec["tools"] = json.loads(rec.pop("tools_json"))
        rec["blocked"] = json.loads(rec.pop("blocked_json"))
        rec["mcp_servers"] = json.loads(rec.pop("mcp_json"))
        rec["enabled"] = bool(rec["enabled"])
        out.append(rec)
    return out


def can_edit(slug: str, user_id: str, user_role: str = "") -> bool:
    if user_role == "admin":
        return True
    with _conn() as c:
        row = c.execute("SELECT owner_id FROM agents WHERE slug=?", (slug,)).fetchone()
        if row and row["owner_id"] == user_id:
            return True
        m = c.execute(
            "SELECT role FROM agent_members WHERE slug=? AND user_id=?", (slug, user_id)
        ).fetchone()
    return bool(m and _ROLE_RANK.get(m["role"], 0) >= _ROLE_RANK["editor"])


def update(slug: str, user_id: str, user_role: str = "", **fields) -> dict | None:
    """Edit an agent in place, keeping its usage history.

    Without this, changing a prompt meant delete-and-recreate, which threw away
    used_count/last_used and broke every audit row that referenced the old slug.
    The slug and owner are deliberately not editable: the slug is the identifier
    schedules and audit rows point at.
    """
    if not can_edit(slug, user_id, user_role):
        return None
    cur = get(slug)
    if not cur:
        return None
    allowed = {"name", "description", "model", "mode", "tools",
               "blocked", "mcp_servers", "enabled", "system_prompt"}
    for k, v in fields.items():
        if k in allowed:
            cur[k] = v
    cur["updated_at"] = _now()
    with _conn() as c:
        c.execute(
            """UPDATE agents SET name=?, description=?, model=?, mode=?, tools_json=?,
               blocked_json=?, mcp_json=?, enabled=?, updated_at=? WHERE slug=?""",
            (cur["name"], cur.get("description", ""), cur.get("model", ""),
             cur.get("mode", "assistant"), json.dumps(cur.get("tools")),
             json.dumps(cur.get("blocked") or []), json.dumps(cur.get("mcp_servers")),
             1 if cur.get("enabled", True) else 0, cur["updated_at"], slug),
        )
    _write_md(cur)
    return cur


def share(slug: str, with_user_id: str, role: str, user_id: str,
          user_role: str = "") -> bool:
    """Give another user access. Only an owner/editor (or admin) may share.

    The agent_members table existed from the start and was honoured by
    can_edit(), but nothing ever wrote to it beyond the owner row — the sharing
    model was real in the schema and unreachable in practice.
    """
    if role not in ("viewer", "editor"):
        return False
    if not can_edit(slug, user_id, user_role):
        return False
    with _conn() as c:
        if not c.execute("SELECT 1 FROM agents WHERE slug=?", (slug,)).fetchone():
            return False
        c.execute(
            "INSERT OR REPLACE INTO agent_members (slug,user_id,role,added_at) VALUES (?,?,?,?)",
            (slug, with_user_id, role, _now()))
    return True


def unshare(slug: str, with_user_id: str, user_id: str, user_role: str = "") -> bool:
    if not can_edit(slug, user_id, user_role):
        return False
    with _conn() as c:
        # The owner row is not a share and must not be removable this way, or an
        # editor could orphan someone else's agent.
        owner = c.execute("SELECT owner_id FROM agents WHERE slug=?", (slug,)).fetchone()
        if owner and owner["owner_id"] == with_user_id:
            return False
        c.execute("DELETE FROM agent_members WHERE slug=? AND user_id=?", (slug, with_user_id))
    return True


def members(slug: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT user_id, role, added_at FROM agent_members WHERE slug=? ORDER BY added_at",
            (slug,)).fetchall()
    return [dict(r) for r in rows]


def delete(slug: str, user_id: str, user_role: str = "") -> bool:
    if not can_edit(slug, user_id, user_role):
        return False
    with _conn() as c:
        c.execute("DELETE FROM agent_members WHERE slug=?", (slug,))
        c.execute("DELETE FROM agents WHERE slug=?", (slug,))
    md = _agent_md(slug)
    if md.exists():
        md.unlink()
        try:
            md.parent.rmdir()
        except OSError:
            pass       # directory not empty — leave whatever else is in it
    return True


def record_invocation(slug: str, user_id: str, username: str, grants: dict[str, Any]) -> None:
    """Record that an agent ran, and what it was permitted to reach.

    The per-request audit row carries `agent_slug`, answering "which agent handled
    this". This answers the harder question when reconstructing a decision later:
    what could it reach AT THAT MOMENT. Grants are re-derived per invocation from
    a file the user can edit and a role that can change, so neither the profile on
    disk nor the role as it stands today is evidence of what applied then.

    Never raises: an audit write is not worth losing the user's answer over, and
    the per-request row lands regardless.
    """
    tools = grants.get("allowed_tools")
    mcp = grants.get("mcp_prefixes")
    detail = (
        f"model={grants.get('model') or 'default'} "
        f"mode={grants.get('mode') or '?'} "
        f"tools={'all' if tools is None else len(tools)} "
        f"mcp={'all' if mcp is None else (','.join(mcp) or 'none')} "
        f"blocked={len(grants.get('blocked_tools') or [])}"
    )
    try:
        from . import audit as _audit
        _audit.log_event(user_id, username, "agent.invoked", detail=detail,
                         target_id=slug, agent_slug=slug)
    except Exception:      # noqa: BLE001 — never fail a request over an audit write
        pass


def mark_used(slug: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE agents SET used_count = used_count + 1, last_used = ? WHERE slug = ?",
            (_now(), slug),
        )
