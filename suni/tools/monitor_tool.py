"""Orchestrator tools for the web/news monitor."""
from __future__ import annotations
from .. import monitor as _monitor

ADD_TOPIC_SCHEMA = {
    "name": "monitor_add_topic",
    "description": "Add a keyword or phrase to the news watch-list. SUNI will alert you when matching news appears.",
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Keyword or phrase to watch, e.g. 'AI regulation EU'"},
        },
        "required": ["topic"],
    },
}

ADD_FEED_SCHEMA = {
    "name": "monitor_add_feed",
    "description": "Subscribe to an RSS or Atom feed URL for monitoring.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "RSS/Atom feed URL"},
        },
        "required": ["url"],
    },
}

LIST_SCHEMA = {
    "name": "monitor_list",
    "description": "List all active watch topics and RSS feeds.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

REMOVE_SCHEMA = {
    "name": "monitor_remove",
    "description": "Remove a topic or feed from the watch-list by its ID.",
    "parameters": {
        "type": "object",
        "properties": {
            "watch_id": {"type": "string", "description": "Watch item ID (from monitor_list)"},
        },
        "required": ["watch_id"],
    },
}

ALERTS_SCHEMA = {
    "name": "monitor_alerts",
    "description": "Show recent news alerts from the monitor. Use to check what was found since last check.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max alerts to return (default 10)", "default": 10},
        },
        "required": [],
    },
}


def add_topic_handler(topic: str) -> str:
    w = _monitor.add_watch("topic", topic)
    return f"Now watching topic: '{topic}' (id: {w['id']})"


def add_feed_handler(url: str) -> str:
    w = _monitor.add_watch("feed", url)
    return f"Now watching feed: {url} (id: {w['id']})"


def list_handler() -> str:
    watches = _monitor.list_watches()
    if not watches:
        return "No active monitors."
    lines = []
    for w in watches:
        status = "active" if w.get("enabled") else "paused"
        lines.append(f"[{w['id']}] {w['type'].upper():5} {w['value']} ({status})")
    return "\n".join(lines)


def remove_handler(watch_id: str) -> str:
    ok = _monitor.remove_watch(watch_id)
    return f"Removed watch '{watch_id}'." if ok else f"Watch '{watch_id}' not found."


def alerts_handler(limit: int = 10) -> str:
    alerts = _monitor.get_recent_alerts(limit=limit)
    if not alerts:
        return "No alerts yet."
    lines = []
    for a in alerts:
        ts   = a.get("created_at", "")[:10]
        notified = " [notified]" if a.get("notified") else " [new]"
        lines.append(f"[{ts}]{notified} {a['title']}")
        if a.get("url"):
            lines.append(f"  {a['url']}")
    return "\n".join(lines)
