"""
SUNIverse Articles tool — answers questions about SUNI's own published articles.

Reads from Suni's RAG memory (the `article`-typed entries ingested by
`ingestion/articles.py` via /ingest-articles), NOT from a live SQL Server.

Why memory instead of the DB: the article DB is a dev-only SQL Server with
hardcoded creds that won't exist in a deployed SUNI. The ingested memory is the
portable source of truth, and it already holds every article Suni knows about.

Recency queries ("what's the latest article", "the last 10 titles") are the whole
reason this tool exists: they're ORDER-BY-id questions that semantic RAG retrieval
cannot answer — a generic meta-question has almost no embedding overlap with any
specific article's text, so it never surfaces the newest one. This tool sorts the
article entries by id_article/date directly, which is what those queries need.
"""
from __future__ import annotations
from collections import Counter

from . import memory_tool

# Articles are GLOBAL SUNI content (her published work), ingested into the global
# memory store — NOT into any per-user store. At a live web request the
# orchestrator binds memory_tool to the *per-user* manager (memory_override in
# _safe_run), whose store holds zero articles. So this tool must read the global
# store directly, bound once at orchestrator build time — never the per-request
# manager. Set via bind_global() in server.py / main.py.
_global_mgr = None


def bind_global(manager) -> None:
    """Bind the global MemoryManager that holds the ingested article memories.
    Called once at orchestrator construction; the same for every user."""
    global _global_mgr
    _global_mgr = manager

CATEGORIES = {
    1:  "AI & Society",
    2:  "Digital Health",
    3:  "Future of Work",
    4:  "Privacy & Identity",
    5:  "Connected Living",
    6:  "Science & Discovery",
    7:  "Digital Culture",
    8:  "Ethics & Power",
    9:  "Education & Learning",
    10: "Money & Economy",
    11: "Gaming",
    12: "Art & AI",
}


def _clean(content: str) -> str:
    """Strip the stored 'SUNI wrote: ' prefix so titles read naturally."""
    return content.removeprefix("SUNI wrote: ").strip()


def _articles() -> list[dict]:
    """All ingested article memories, newest first (by id_article, then date).

    Reads the global store (bound via bind_global). Falls back to the per-request
    manager only if no global was bound — e.g. an embedding context where the
    single manager IS the global store."""
    mgr = _global_mgr if _global_mgr is not None else memory_tool._mgr()
    store = getattr(mgr, "store", None) if mgr is not None else None
    if store is None:
        return []
    arts = store.get_by_type("article")

    def _key(e: dict):
        meta = e.get("metadata") or {}
        return (meta.get("id_article") or 0, meta.get("date") or "")

    return sorted(arts, key=_key, reverse=True)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

RECENT_SCHEMA = {
    "name": "get_recent_articles",
    "description": (
        "Get SUNI's most recently published articles from SUNIverse (from your "
        "ingested article memory). Use this for any question about your latest / "
        "last / recent published articles or their titles. Report only the titles "
        "this returns — never invent one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "n": {
                "type": "integer",
                "description": "Number of recent articles to return (default 10, max 30)",
            },
            "category_id": {
                "type": "integer",
                "description": (
                    "Optional category filter: 1=AI & Society, 2=Digital Health, "
                    "3=Future of Work, 4=Privacy & Identity, 5=Connected Living, "
                    "6=Science & Discovery, 7=Digital Culture, 8=Ethics & Power, "
                    "9=Education & Learning, 10=Money & Economy, 11=Gaming, 12=Art & AI"
                ),
            },
        },
    },
}

SEARCH_SCHEMA = {
    "name": "search_articles",
    "description": (
        "Search SUNI's published articles by keyword (titles and subtitles). "
        "Use this when asked about specific topics SUNI has covered. Report only "
        "the actual matches — never invent titles."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword or phrase to search for",
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (default 8)",
            },
        },
        "required": ["query"],
    },
}

STATS_SCHEMA = {
    "name": "get_article_stats",
    "description": (
        "Get statistics about the SUNIverse articles SUNI has ingested into memory: "
        "count known, breakdown by category, and most recent publication date."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

_EMPTY = (
    "No published articles are in my memory yet. "
    "Run /ingest-articles to load them from SUNIverse."
)


def get_recent_articles(n: int = 10, category_id: int | None = None) -> str:
    try:
        n = min(max(1, int(n or 10)), 30)
    except (TypeError, ValueError):
        n = 10
    arts = _articles()
    if category_id:
        cat_name = CATEGORIES.get(int(category_id))
        if cat_name:
            arts = [a for a in arts if (a.get("metadata") or {}).get("category") == cat_name]
    arts = arts[:n]
    if not arts:
        return _EMPTY
    lines = [f"{i}. {_clean(a['content'])}" for i, a in enumerate(arts, 1)]
    return "SUNI's most recent published articles:\n" + "\n".join(lines)


def search_articles(query: str, limit: int = 8) -> str:
    try:
        limit = min(max(1, int(limit or 8)), 20)
    except (TypeError, ValueError):
        limit = 8
    q = (query or "").strip().lower()
    if not q:
        return "Provide a keyword to search articles for."
    matches = [a for a in _articles() if q in a["content"].lower()][:limit]
    if not matches:
        return f"No published articles in my memory match '{query}'."
    lines = [f"- {_clean(a['content'])}" for a in matches]
    return f"Articles matching '{query}':\n" + "\n".join(lines)


def get_article_stats() -> str:
    arts = _articles()
    if not arts:
        return _EMPTY
    cats = Counter((a.get("metadata") or {}).get("category", "Unknown") for a in arts)
    latest = (arts[0].get("metadata") or {}).get("date", "")
    lines = [f"Published articles in my memory: {len(arts)}"]
    if latest:
        lines.append(f"Most recent: {latest}")
    lines.append("\nBy category:")
    for cat, cnt in cats.most_common():
        lines.append(f"  {cat}: {cnt}")
    return "\n".join(lines)
