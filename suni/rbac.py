"""
Role-based access control for SUNI.

Defines which tools each role can use and what conversation modes are available.
Permissions are stored in memory/role_config.json and editable via the admin panel.
Defaults are applied if the file is absent or a role is missing.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path("memory/role_config.json")

# ── Default permissions ───────────────────────────────────────────────────────
# blocked: explicit denylist (applied when allowed is None).
# allowed: explicit allowlist (None means all non-blocked tools).
# mcp_prefixes: MCP server prefixes included in tool list (None = all, [] = none).
# modes: conversation modes available to this role.
# default_mode: which mode the role starts in.

DEFAULTS: dict[str, dict[str, Any]] = {
    "read-only": {
        "allowed": [
            "web_search", "web_fetch",
            "read_file", "list_files",
            "list_emails", "read_email",
            "search_knowledge_base",
            "get_recent_articles", "search_articles", "get_article_stats",
            "skills_list", "skill_view",
        ],
        "blocked": [],
        "mcp_prefixes": [],
        "modes": ["read-only"],
        "default_mode": "read-only",
    },
    "standard": {
        "allowed": None,
        "blocked": [
            "run_shell",
            "claude_task", "claude_code", "claude_create_agents",
            "claude_schedule_task", "claude_advisor", "claude_init_project",
            "skill_save", "skill_delete",
        ],
        "mcp_prefixes": [],
        "modes": ["assistant", "read-only"],
        "default_mode": "assistant",
    },
    "power-user": {
        "allowed": None,
        "blocked": [],
        "mcp_prefixes": ["filesystem", "playwright"],
        "modes": ["assistant", "task", "read-only", "collaborate"],
        "default_mode": "assistant",
    },
    "admin": {
        "allowed": None,
        "blocked": [],
        "mcp_prefixes": None,   # None = all registered prefixes
        "modes": ["assistant", "task", "read-only", "collaborate"],
        "default_mode": "assistant",
    },
}

# "collaborate" = Mode 2 (multi-model frontier collaboration): power-user/admin
# only — it is cloud-based and costly, so the channel-facing "standard" role and
# "read-only" do NOT get it.
VALID_MODES = ["assistant", "task", "read-only", "collaborate"]

_cache: dict[str, dict] = {}


def load() -> dict[str, dict]:
    global _cache
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        # Merge with defaults so missing keys fall back
        _cache = {role: {**DEFAULTS.get(role, {}), **data.get(role, {})}
                  for role in DEFAULTS}
        # Include any custom roles from the file
        for role, cfg in data.items():
            if role not in _cache:
                _cache[role] = cfg
    except Exception:
        _cache = {r: dict(v) for r, v in DEFAULTS.items()}
    return _cache


def save(config: dict[str, dict]) -> None:
    global _cache
    _CONFIG_PATH.parent.mkdir(exist_ok=True)
    _CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _cache = config


def get_role(role: str) -> dict:
    if not _cache:
        load()
    return _cache.get(role, DEFAULTS.get(role, DEFAULTS["standard"]))


def all_roles() -> dict[str, dict]:
    if not _cache:
        load()
    return dict(_cache)


def allowed_tools(role: str) -> list[str] | None:
    """None means all tools allowed (subject to blocked list)."""
    return get_role(role).get("allowed")


def blocked_tools(role: str) -> list[str]:
    return get_role(role).get("blocked", [])


def mcp_prefixes(role: str, all_registered: list[str]) -> list[str] | None:
    """Return the MCP prefix list for this role.
    None means include all registered prefixes.
    """
    cfg = get_role(role).get("mcp_prefixes")
    if cfg is None:
        return all_registered   # admin gets all
    return cfg


def allowed_modes(role: str) -> list[str]:
    return get_role(role).get("modes", ["assistant"])


# ── Memory clearance (enterprise memory governance, Phase 1) ──────────────────
# Coarse read-clearance labels for global/collective memory. An entry's
# `visibility` must be in the caller's clearance set to be retrievable.
# Phase 1 is coarse (org / restricted); dept/role granularity is Phase 3.
def clearance_for_role(role: str) -> set[str]:
    """Global-memory visibility labels this role may read."""
    if role in ("admin", "power-user"):
        return {"org", "restricted"}
    return {"org"}   # standard / read-only: company-shared org memory only


def default_mode(role: str) -> str:
    return get_role(role).get("default_mode", "assistant")


def can_use_mode(role: str, mode: str) -> bool:
    return mode in allowed_modes(role)


# Load on import
load()
