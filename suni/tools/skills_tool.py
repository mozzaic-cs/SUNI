"""
Skills tools — SUNI's procedural memory interface.

Four tools exposed to the model:
  skills_list   — Level 0: name + description for all skills (or by category/search)
  skill_view    — Level 1: full SKILL.md body for a specific skill
  skill_save    — Create or update a skill (auto-generation uses this)
  skill_delete  — Remove a skill (admin/power-user only)
"""
from __future__ import annotations

_store = None   # SkillStore; set by set_store() at startup


def set_store(store) -> None:
    global _store
    _store = store


# ── schemas ───────────────────────────────────────────────────────────────────

LIST_SCHEMA = {
    "name": "skills_list",
    "description": (
        "List SUNI's learned skills (Level-0: name + description only, no body). "
        "Use this to discover what skills are available before loading one. "
        "Filter by category or search keywords for faster lookup."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Filter by category (e.g. 'reporting', 'web', 'files'). Leave empty for all.",
            },
            "search": {
                "type": "string",
                "description": "Keyword or phrase to search skill names and descriptions.",
            },
        },
        "required": [],
    },
}

VIEW_SCHEMA = {
    "name": "skill_view",
    "description": (
        "Load the full content of a specific skill (Level-1: complete SKILL.md with steps). "
        "Call this when you've identified a relevant skill via skills_list and want to follow its procedure. "
        "Increments the skill's usage counter."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": "The skill slug (from skills_list or [SKILL:slug] tag in context)",
            },
        },
        "required": ["slug"],
    },
}

SAVE_SCHEMA = {
    "name": "skill_save",
    "description": (
        "Create or update a skill in SUNI's procedural memory. "
        "Use this after completing a complex task to save the approach for future reuse. "
        "Write a clear description and step-by-step procedure so future sessions can follow it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Human-readable skill name (e.g. 'Generate your organization Weekly Report')",
            },
            "description": {
                "type": "string",
                "description": "One-sentence description of what this skill does (shown in Level-0 list)",
            },
            "category": {
                "type": "string",
                "description": "Category slug (e.g. 'reporting', 'web', 'files', 'communication', 'analysis')",
                "default": "general",
            },
            "body": {
                "type": "string",
                "description": "Full SKILL.md body — what this skill does, steps, example tool calls, tips",
            },
            "tool_count": {
                "type": "integer",
                "description": "Number of tool calls the original task required (for complexity tracking)",
                "default": 0,
            },
        },
        "required": ["name", "description", "body"],
    },
}

DELETE_SCHEMA = {
    "name": "skill_delete",
    "description": "Delete a skill from SUNI's procedural memory. Use with care — this is permanent.",
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Slug of the skill to delete"},
        },
        "required": ["slug"],
    },
}


# ── handlers ──────────────────────────────────────────────────────────────────

def list_handler(category: str = "", search: str = "") -> str:
    if _store is None:
        return "Skills system not initialised."
    if search:
        skills = _store.search(search, top_k=20)
    else:
        skills = _store.list(category=category)
    if not skills:
        return "No skills found." + (f" (category={category!r})" if category else "")
    lines = [f"Skills ({len(skills)} found):\n"]
    for s in skills:
        used = f"used {s['used_count']}×" if s.get("used_count") else "never used"
        lines.append(f"  [{s['slug']}]  {s['name']}  ({s['category']})  {used}")
        if s.get("description"):
            lines.append(f"    {s['description']}")
    return "\n".join(lines)


def view_handler(slug: str) -> str:
    if _store is None:
        return "Skills system not initialised."
    skill = _store.get(slug)
    if not skill:
        return f"Skill '{slug}' not found. Use skills_list to see available skills."
    return (
        f"# {skill['name']}  (v{skill['version']}, {skill['category']})\n"
        f"Used {skill['used_count']}× | Created {skill.get('created_at', '')[:10]}\n\n"
        + skill.get("body", "(no content)")
    )


def save_handler(name: str, description: str, body: str,
                 category: str = "general", tool_count: int = 0) -> str:
    if _store is None:
        return "Skills system not initialised."
    try:
        slug = _store.save(
            name=name, body=body, category=category,
            description=description, tool_count=tool_count,
        )
        return f"Skill saved: [{slug}] {name}"
    except Exception as e:
        return f"Failed to save skill: {e}"


def delete_handler(slug: str) -> str:
    if _store is None:
        return "Skills system not initialised."
    ok = _store.delete(slug)
    return f"Skill '{slug}' deleted." if ok else f"Skill '{slug}' not found."
