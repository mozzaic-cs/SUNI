"""
Ingests SUNI's own published articles from the SUNIverse SQL Server database
into Suni's RAG memory.

Each article is stored as: "SUNI wrote: '{title}'. {subtitle}. [{category}] {date}."

Tracking is done by IDArticle — only new articles are embedded on subsequent runs.
Ingest in batches to keep embedding time reasonable.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from ..notifications.email_notify import notify_new_articles, is_configured

# NOTE: pyodbc is imported lazily inside _connect(), never at module scope.
# main.py imports this module at startup, so a module-level import made the
# whole application unstartable wherever pyodbc could not load — and its wheel
# installs fine on Linux while still failing at import without the unixODBC
# runtime (libodbc.so.2). Article ingestion is an optional integration; it must
# not be able to stop SUNI from booting.

# Connection details come from the environment (.env) — never hardcoded here.
# Built lazily inside _conn_str() rather than at import: this module is imported
# at startup by main.py, so a missing credential must fail at connect time, not
# prevent SUNI from starting.
# Only vendor-generic values get defaults. The database name is instance data,
# so it is required rather than defaulted — a default would hardcode one
# deployment's schema name into a public repo.
_DB_ENV_DEFAULTS = {
    "SUNIVERSE_DB_DRIVER": "{ODBC Driver 17 for SQL Server}",
    "SUNIVERSE_DB_PORT":   "1433",
}


def _conn_str() -> str:
    """Assemble the SQL Server connection string from the environment.

    Raises RuntimeError with an actionable message if a required value is unset,
    so an unconfigured install gets a clear error instead of a driver-level one.
    """
    env  = {k: os.environ.get(k) or v for k, v in _DB_ENV_DEFAULTS.items()}
    host = os.environ.get("SUNIVERSE_DB_HOST")
    name = os.environ.get("SUNIVERSE_DB_NAME")
    user = os.environ.get("SUNIVERSE_DB_USER")
    pwd  = os.environ.get("SUNIVERSE_DB_PASS")
    missing = [n for n, v in (("SUNIVERSE_DB_HOST", host),
                              ("SUNIVERSE_DB_NAME", name),
                              ("SUNIVERSE_DB_USER", user),
                              ("SUNIVERSE_DB_PASS", pwd)) if not v]
    if missing:
        raise RuntimeError(
            "Article ingestion is not configured — missing "
            + ", ".join(missing)
            + ". Set these in .env (see .env.example); article ingestion needs "
              "the SUNIverse SQL Server and is a dev-environment feature."
        )
    return (
        f"DRIVER={env['SUNIVERSE_DB_DRIVER']};"
        f"SERVER={host},{env['SUNIVERSE_DB_PORT']};"
        f"DATABASE={name};"
        f"UID={user};"
        f"PWD={pwd}"
    )

CATEGORIES = {
    1: "AI & Society", 2: "Digital Health", 3: "Future of Work",
    4: "Privacy & Identity", 5: "Connected Living", 6: "Science & Discovery",
    7: "Digital Culture", 8: "Ethics & Power", 9: "Education & Learning",
    10: "Money & Economy", 11: "Gaming", 12: "Art & AI",
}

ARTICLE_STATE_FILE = Path("memory/article_ingest_state.json")


def _load_state() -> set[int]:
    if ARTICLE_STATE_FILE.exists():
        return set(json.loads(ARTICLE_STATE_FILE.read_text(encoding="utf-8")))
    return set()


def _save_state(ingested_ids: set[int]) -> None:
    ARTICLE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ARTICLE_STATE_FILE.write_text(
        json.dumps(sorted(ingested_ids)), encoding="utf-8"
    )


def _format_article(title: str, subtitle: str, cat_id: int, date) -> str:
    cat = CATEGORIES.get(cat_id, "Unknown")
    date_str = str(date)[:10] if date else "unknown date"
    sub = f" {subtitle}." if subtitle and subtitle.strip() else ""
    return f"SUNI wrote: '{title}'.{sub} [{cat}] Published {date_str}."


def _connect():
    """Open the SQL Server connection, importing the driver only when needed.

    Raises RuntimeError with an actionable message when pyodbc is unavailable,
    rather than an ImportError from deep in the call stack.
    """
    try:
        import pyodbc
    except ImportError as e:
        raise RuntimeError(
            "Article ingestion needs the optional 'pyodbc' package and the "
            "unixODBC runtime. Install with: pip install -r "
            "requirements-articles.txt (on Debian/Ubuntu also: apt install "
            f"unixodbc). Original error: {e}"
        ) from e
    return pyodbc.connect(_conn_str(), timeout=10)


def fetch_articles(limit: int = 300, offset: int = 0) -> list[dict]:
    """Fetch published articles from SQL Server, newest first."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT IDArticle, ArticleTitle, ArticleSubTitle, IDCat, ArticleDate "
        "FROM SUNI_Articles WHERE ArticleStatus=1 "
        "ORDER BY IDArticle DESC "
        "OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",
        (offset, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "title": r[1] or "",
            "subtitle": r[2] or "",
            "cat_id": r[3],
            "date": r[4],
        }
        for r in rows
    ]


async def ingest_articles(memory_manager, limit: int = 300, force: bool = False) -> dict:
    """
    Ingest up to `limit` recent published articles into Suni's RAG memory.
    Skips already-ingested article IDs unless force=True.

    Returns stats: {ingested, skipped, total_fetched}
    """
    ingested_ids = set() if force else _load_state()
    articles     = fetch_articles(limit=limit)
    stats        = {"ingested": 0, "skipped": 0, "total_fetched": len(articles)}
    newly_added  = []

    for art in articles:
        if art["id"] in ingested_ids:
            stats["skipped"] += 1
            continue

        text = _format_article(art["title"], art["subtitle"], art["cat_id"], art["date"])
        await memory_manager.add(
            text,
            memory_type="article",
            metadata={
                "id_article": art["id"],
                "category":   CATEGORIES.get(art["cat_id"], "Unknown"),
                "date":       str(art["date"])[:10] if art["date"] else "",
            },
        )
        ingested_ids.add(art["id"])
        stats["ingested"] += 1
        newly_added.append({
            "title":    art["title"],
            "subtitle": art["subtitle"],
            "category": CATEGORIES.get(art["cat_id"], "Unknown"),
            "date":     str(art["date"])[:10] if art["date"] else "",
        })

    _save_state(ingested_ids)

    if newly_added and is_configured():
        try:
            notify_new_articles(newly_added)
        except Exception:
            pass  # never let email failure break ingestion

    return stats
