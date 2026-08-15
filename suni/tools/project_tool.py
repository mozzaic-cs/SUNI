"""Orchestrator tools for persistent projects."""
from __future__ import annotations
from .. import projects as _projects

CREATE_SCHEMA = {
    "name": "project_create",
    "description": "Create a new persistent project with a name and goal. Returns the project ID.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short project name"},
            "goal": {"type": "string", "description": "What this project aims to achieve"},
        },
        "required": ["name"],
    },
}

LIST_SCHEMA = {
    "name": "project_list",
    "description": "List active (and optionally all) projects for the current user.",
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Filter by status: active, paused, done, or all (default: active)",
                "default": "active",
            },
        },
        "required": [],
    },
}

GET_SCHEMA = {
    "name": "project_get",
    "description": "Get full details of a project including context, files, contacts, and recent actions.",
    "parameters": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID (from project_list)"},
        },
        "required": ["project_id"],
    },
}

LOG_SCHEMA = {
    "name": "project_log",
    "description": "Append an action or progress note to a project's action log.",
    "parameters": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "action":     {"type": "string", "description": "What was done or decided"},
        },
        "required": ["project_id", "action"],
    },
}

UPDATE_SCHEMA = {
    "name": "project_update",
    "description": "Update a project's name, goal, or status.",
    "parameters": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "name":       {"type": "string"},
            "goal":       {"type": "string"},
            "status":     {"type": "string", "description": "active | paused | done"},
        },
        "required": ["project_id"],
    },
}

SHARE_SCHEMA = {
    "name": "project_share",
    "description": (
        "Share a project with another user or update their access level. "
        "Only the project owner can share it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
            "username":   {"type": "string", "description": "Username to share with"},
            "role": {
                "type": "string",
                "description": "Access level: editor (can read/write) or viewer (read-only)",
                "enum": ["editor", "viewer"],
                "default": "editor",
            },
        },
        "required": ["project_id", "username"],
    },
}

MEMBERS_SCHEMA = {
    "name": "project_members",
    "description": "List all members of a project and their roles.",
    "parameters": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID"},
        },
        "required": ["project_id"],
    },
}


def create_handler(name: str, goal: str = "", _user_id: str = "") -> str:
    p = _projects.create_project(name, goal, user_id=_user_id)
    return f"Project created: [{p['id']}] {p['name']}" + (f"\nGoal: {goal}" if goal else "")


def list_handler(status: str = "active", _user_id: str = "") -> str:
    filter_status = "" if status == "all" else status
    projects = _projects.list_projects(user_id=_user_id, status=filter_status)
    if not projects:
        return f"No {'active' if filter_status == 'active' else ''} projects."
    lines = []
    for p in projects:
        ts = p.get("updated_at", "")[:10]
        my_role = p.get("my_role", "")
        role_tag = f" [{my_role}]" if my_role else ""
        lines.append(
            f"[{p['id']}] {p['status'].upper():6} {p['name']}{role_tag} (updated {ts})"
        )
        if p.get("goal"):
            lines.append(f"  Goal: {p['goal'][:80]}")
    return "\n".join(lines)


def get_handler(project_id: str) -> str:
    p = _projects.get_project(project_id)
    if not p:
        return f"Project '{project_id}' not found."
    return _projects.build_context_block(p)


def log_handler(project_id: str, action: str) -> str:
    result = _projects.add_action(project_id, action)
    if not result:
        return f"Project '{project_id}' not found."
    return f"Logged to [{project_id}] {result['name']}: {action[:80]}"


def update_handler(
    project_id: str,
    name: str | None = None,
    goal: str | None = None,
    status: str | None = None,
) -> str:
    if status and status not in ("active", "paused", "done"):
        return "status must be one of: active, paused, done"
    result = _projects.update_project(project_id, name, goal, status)
    if not result:
        return f"Project '{project_id}' not found."
    return f"Project updated: [{result['id']}] {result['name']} — {result['status']}"


def share_handler(project_id: str, username: str, role: str = "editor") -> str:
    from .. import auth as _auth
    target = _auth.get_user_by_username(username)
    if not target:
        return f"User '{username}' not found."
    ok = _projects.add_member(project_id, target["id"], role=role)
    if not ok:
        return f"Project '{project_id}' not found or invalid role."
    return f"Shared project [{project_id}] with {username} as {role}."


def members_handler(project_id: str) -> str:
    members = _projects.get_members(project_id)
    if not members:
        return f"Project '{project_id}' not found or has no members."
    lines = [f"Members of [{project_id}]:"]
    for m in members:
        lines.append(f"  {m['user_id']}  {m['role']:6}  (added {m['added_at'][:10]})")
    return "\n".join(lines)
