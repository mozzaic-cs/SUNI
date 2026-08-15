"""
Model-facing structured-memory tools: memory_save / memory_search /
memory_delete / memory_list.

These let SUNI deliberately record durable, named facts (distinct from the
automatic episodic memory that the consolidator derives from conversation).
Taxonomy mirrors the wider memory system: user | project | feedback | reference.

Per-user binding: the current request's MemoryManager is held in a ContextVar,
bound by the orchestrator at run() entry. Requests for different users run as
separate async tasks, so each sees its own manager — a module global would race
across users. Crucially, user identity comes from this server-set context, NEVER
a tool argument: the model cannot pass another user's id to read/write their
store.

Split into four rigid-schema tools rather than one action-enum tool: small local
models call narrow, unambiguous schemas far more reliably.
"""
from __future__ import annotations
from contextvars import ContextVar

# Current request's MemoryManager (set by the orchestrator, reset in finally).
_current_mgr: ContextVar[object | None] = ContextVar("suni_memory_mgr", default=None)


def bind(manager):
    """Bind the current user's MemoryManager for this async task. Returns a token
    to pass to reset() in a finally block."""
    return _current_mgr.set(manager)


def reset(token) -> None:
    try:
        _current_mgr.reset(token)
    except Exception:
        pass


def _mgr():
    return _current_mgr.get()


def current():
    """The MemoryManager bound for THIS async task (set race-free at request
    start by bind(), before any await), or None if unbound. The orchestrator's
    own memory reads use this instead of a shared instance attribute, so
    concurrent requests with different memory managers never cross."""
    return _current_mgr.get()


# ── Schemas ───────────────────────────────────────────────────────────────────

SAVE_SCHEMA = {
    "name": "memory_save",
    "description": (
        "Save or update a durable, named memory about the user or ongoing work — "
        "something worth remembering across conversations (a preference, a project "
        "detail, a standing instruction, a reference fact). Saving the same name "
        "again UPDATES it (no duplicates). Use this deliberately for facts that "
        "should persist; do not save transient chit-chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short stable identifier for this memory, e.g. 'preferred-language' or 'q3-deadline'. Reusing a name overwrites it.",
            },
            "content": {
                "type": "string",
                "description": "The fact to remember, stated plainly and self-contained.",
            },
            "category": {
                "type": "string",
                "enum": ["user", "project", "feedback", "reference"],
                "description": "user=about the person; project=ongoing work; feedback=how to work with them; reference=external facts/pointers.",
            },
        },
        "required": ["name", "content"],
    },
}

SEARCH_SCHEMA = {
    "name": "memory_search",
    "description": (
        "Search your saved durable memories (from memory_save) by meaning. Use "
        "this to recall what you previously noted about the user or their work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What you are trying to recall."},
            "top_k": {"type": "integer", "description": "Max results (1–10, default 5)."},
        },
        "required": ["query"],
    },
}

DELETE_SCHEMA = {
    "name": "memory_delete",
    "description": "Delete a durable memory by its name (reversible soft-delete).",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The name of the memory to delete."},
        },
        "required": ["name"],
    },
}

LIST_SCHEMA = {
    "name": "memory_list",
    "description": "List all your saved durable memories (names + categories).",
    "parameters": {"type": "object", "properties": {}},
}


# ── Handlers ──────────────────────────────────────────────────────────────────

async def save_handler(name: str = "", content: str = "", category: str = "reference") -> str:
    mgr = _mgr()
    if mgr is None:
        return "Memory is not available right now."
    try:
        res = await mgr.save_memory(name, content, category=category)
        return f"Memory {res['action']}: '{res['name']}' [{res['category']}]."
    except ValueError as e:
        return f"Could not save memory: {e}"
    except Exception as e:
        return f"Memory save error: {e}"


async def search_handler(query: str = "", top_k: int = 5) -> str:
    mgr = _mgr()
    if mgr is None:
        return "Memory is not available right now."
    try:
        top_k = min(max(1, int(top_k)), 10)
    except Exception:
        top_k = 5
    try:
        results = await mgr.search_memory(query, top_k=top_k)
    except Exception as e:
        return f"Memory search error: {e}"
    if not results:
        return f"No saved memories match: {query!r}"
    lines = [f"Saved memories for {query!r}:"]
    for m in results:
        meta = m.get("metadata") or {}
        lines.append(f"- [{meta.get('category', 'reference')}:{meta.get('name', '')}] {m['content']}")
    return "\n".join(lines)


async def delete_handler(name: str = "") -> str:
    mgr = _mgr()
    if mgr is None:
        return "Memory is not available right now."
    if not (name or "").strip():
        return "A memory name is required to delete."
    try:
        ok = mgr.delete_memory(name)
    except Exception as e:
        return f"Memory delete error: {e}"
    return f"Deleted memory '{name.strip().lower()}'." if ok else f"No memory named '{name.strip().lower()}'."


async def list_handler() -> str:
    mgr = _mgr()
    if mgr is None:
        return "Memory is not available right now."
    try:
        items = mgr.list_memory()
    except Exception as e:
        return f"Memory list error: {e}"
    if not items:
        return "No durable memories saved yet."
    lines = [f"Saved memories ({len(items)}):"]
    for m in items:
        lines.append(f"- [{m['category']}] {m['name']}: {m['content'][:100]}")
    return "\n".join(lines)
