"""
Web / news monitoring for SUNI.

Watches a list of topics (keyword searches) and RSS feed URLs.
Deduplicates by URL; notifies via Telegram/WhatsApp when new matches appear.
Stores state in memory/monitor.db.
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    pass

log = logging.getLogger("suni.monitor")

_DB = Path("memory/monitor.db")
_DEFAULT_INTERVAL = 3600   # seconds between checks

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS watch_items (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,   -- 'topic' | 'feed'
    value      TEXT NOT NULL,   -- keyword or RSS URL
    user_id    TEXT DEFAULT '',
    enabled    INTEGER DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_urls (
    url        TEXT PRIMARY KEY,
    title      TEXT,
    watch_id   TEXT,
    found_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id         TEXT PRIMARY KEY,
    watch_id   TEXT,
    title      TEXT,
    url        TEXT,
    summary    TEXT,
    notified   INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_notified ON alerts(notified);
CREATE INDEX IF NOT EXISTS idx_alerts_created  ON alerts(created_at DESC);
"""


def _conn() -> sqlite3.Connection:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA_SQL)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Watch-list management ────────────────────────────────────────────────────

def add_watch(type_: str, value: str, user_id: str = "") -> dict:
    import uuid
    wid = str(uuid.uuid4())[:8]
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watch_items (id, type, value, user_id, enabled, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (wid, type_, value.strip(), user_id, now),
        )
    return {"id": wid, "type": type_, "value": value}


def remove_watch(watch_id: str) -> bool:
    conn = _conn()
    cur = conn.execute("DELETE FROM watch_items WHERE id = ?", (watch_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def list_watches(user_id: str = "") -> list[dict]:
    conn = _conn()
    if user_id:
        rows = conn.execute(
            "SELECT * FROM watch_items WHERE user_id = ? OR user_id = '' ORDER BY created_at",
            (user_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM watch_items ORDER BY created_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_pending_alerts(limit: int = 20) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM alerts WHERE notified = 0 ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_alerts(limit: int = 50) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_notified(alert_ids: list[str]) -> None:
    if not alert_ids:
        return
    with _conn() as conn:
        conn.executemany(
            "UPDATE alerts SET notified = 1 WHERE id = ?",
            [(aid,) for aid in alert_ids],
        )


def _seen(url: str) -> bool:
    conn = _conn()
    exists = conn.execute("SELECT 1 FROM seen_urls WHERE url = ?", (url,)).fetchone()
    conn.close()
    return exists is not None


def _record_seen(url: str, title: str, watch_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_urls (url, title, watch_id, found_at) VALUES (?, ?, ?, ?)",
            (url, title, watch_id, _now()),
        )


def _save_alert(watch_id: str, title: str, url: str, summary: str) -> str:
    import uuid
    aid = str(uuid.uuid4())[:8]
    with _conn() as conn:
        conn.execute(
            "INSERT INTO alerts (id, watch_id, title, url, summary, notified, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (aid, watch_id, title, url, summary, _now()),
        )
    return aid


# ── RSS parsing ──────────────────────────────────────────────────────────────

async def _fetch_rss(url: str) -> list[dict]:
    """Minimal RSS/Atom parser — returns [{title, link, summary}]."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "SUNI-Monitor/1.0"})
            r.raise_for_status()
            xml = r.text
    except Exception as e:
        log.warning("[MONITOR] RSS fetch failed %s: %s", url, e)
        return []

    items = []
    # Match <item> or <entry> blocks
    for block in re.findall(r'<(?:item|entry)>(.*?)</(?:item|entry)>', xml, re.DOTALL):
        title_m = re.search(r'<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>', block, re.DOTALL)
        link_m  = (
            re.search(r'<link[^>]*href=["\']([^"\']+)["\']', block)
            or re.search(r'<link[^>]*>(https?://[^<]+)</link>', block)
        )
        desc_m  = re.search(
            r'<(?:description|summary)[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</(?:description|summary)>',
            block, re.DOTALL
        )
        if title_m and link_m:
            title   = re.sub(r'<[^>]+>', '', title_m.group(1)).strip()
            link    = link_m.group(1).strip()
            summary = re.sub(r'<[^>]+>', '', (desc_m.group(1) if desc_m else "")).strip()[:300]
            items.append({"title": title, "link": link, "summary": summary})
    return items


# ── Topic search via DuckDuckGo instant answer ──────────────────────────────

async def _search_news(query: str) -> list[dict]:
    """Query DuckDuckGo News API (free, no key required)."""
    try:
        params = {"q": query, "format": "json", "t": "h_"}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://api.duckduckgo.com/",
                params=params,
                headers={"User-Agent": "SUNI-Monitor/1.0"},
            )
            data = r.json()
    except Exception as e:
        log.warning("[MONITOR] DDG search failed for %r: %s", query, e)
        return []

    items = []
    for topic in data.get("RelatedTopics", []):
        url  = topic.get("FirstURL", "")
        text = topic.get("Text", "")
        if url and text:
            items.append({"title": text[:100], "link": url, "summary": text[:300]})
    return items[:10]


# ── Main check loop ──────────────────────────────────────────────────────────

async def run_check() -> list[dict]:
    """Run one monitoring cycle. Returns list of new alert dicts."""
    watches = list_watches()
    new_alerts = []

    for w in watches:
        if not w.get("enabled"):
            continue

        wtype = w["type"]
        value = w["value"]
        wid   = w["id"]

        if wtype == "feed":
            entries = await _fetch_rss(value)
        elif wtype == "topic":
            entries = await _search_news(value)
        else:
            continue

        for entry in entries:
            url   = entry.get("link", "")
            title = entry.get("title", "")
            if not url or _seen(url):
                continue
            _record_seen(url, title, wid)
            aid = _save_alert(wid, title, url, entry.get("summary", ""))
            new_alerts.append({
                "id": aid, "watch_id": wid, "watch_value": value,
                "title": title, "url": url, "summary": entry.get("summary", ""),
            })
            log.info("[MONITOR] new hit for %r: %s", value, title[:80])

    return new_alerts


# ── Background service ───────────────────────────────────────────────────────

_stop_event: asyncio.Event | None = None


async def start_monitor(interval: int = _DEFAULT_INTERVAL) -> None:
    """Long-running background task. Sends Telegram notifications on hits."""
    global _stop_event
    _stop_event = asyncio.Event()

    log.info("[MONITOR] started (interval=%ds)", interval)
    while not _stop_event.is_set():
        try:
            new_alerts = await run_check()
            if new_alerts:
                await _push_alerts(new_alerts)
        except Exception as e:
            log.error("[MONITOR] check failed: %s", e, exc_info=True)

        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass   # normal — time to run next check


async def _push_alerts(alerts: list[dict]) -> None:
    """Push new alerts via Telegram (if configured)."""
    try:
        from .telegram.handler import send_reply as tg_send, is_configured as tg_ok
        from . import config as _cfg
    except ImportError:
        return

    if not tg_ok():
        return

    tg_chat = _cfg.get("telegram_notify_chat_id", "")
    if not tg_chat:
        return

    lines = [f"**Monitor alert — {len(alerts)} new hit(s)**\n"]
    for a in alerts[:5]:   # cap at 5 to avoid message spam
        lines.append(f"• [{a['title']}]({a['url']})")
        if a.get("summary"):
            lines.append(f"  {a['summary'][:120]}…")
    if len(alerts) > 5:
        lines.append(f"…and {len(alerts) - 5} more.")

    try:
        await tg_send(tg_chat, "\n".join(lines))
        mark_notified([a["id"] for a in alerts])
    except Exception as e:
        log.error("[MONITOR] push failed: %s", e)


def stop_monitor() -> None:
    if _stop_event:
        _stop_event.set()
