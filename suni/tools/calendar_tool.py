"""Orchestrator tools for CalDAV calendar integration."""
from __future__ import annotations
from .. import calendar_client as _cal

LIST_EVENTS_SCHEMA = {
    "name": "calendar_list_events",
    "description": (
        "List upcoming calendar events. "
        "Requires caldav_url, caldav_username, caldav_password in SUNI config."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "How many days ahead to look (default 7)",
                "default": 7,
            },
        },
        "required": [],
    },
}

CREATE_EVENT_SCHEMA = {
    "name": "calendar_create_event",
    "description": (
        "Create a new calendar event. "
        "start and end must be ISO 8601 strings with timezone, e.g. '2026-06-10T10:00:00+01:00'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "summary":     {"type": "string", "description": "Event title"},
            "start":       {"type": "string", "description": "Start datetime (ISO 8601)"},
            "end":         {"type": "string", "description": "End datetime (ISO 8601)"},
            "location":    {"type": "string", "description": "Location (optional)"},
            "description": {"type": "string", "description": "Event description (optional)"},
        },
        "required": ["summary", "start", "end"],
    },
}

FREE_SLOTS_SCHEMA = {
    "name": "calendar_free_slots",
    "description": "Find free time slots on a given date for scheduling meetings.",
    "parameters": {
        "type": "object",
        "properties": {
            "date":             {"type": "string", "description": "Date in YYYY-MM-DD format"},
            "duration_minutes": {"type": "integer", "description": "Slot length in minutes (default 60)", "default": 60},
            "start_hour":       {"type": "integer", "description": "Working hours start (default 9)", "default": 9},
            "end_hour":         {"type": "integer", "description": "Working hours end (default 18)", "default": 18},
        },
        "required": ["date"],
    },
}

DELETE_EVENT_SCHEMA = {
    "name": "calendar_delete_event",
    "description": "Delete a calendar event by its UID (from calendar_list_events).",
    "parameters": {
        "type": "object",
        "properties": {
            "uid": {"type": "string", "description": "Event UID"},
        },
        "required": ["uid"],
    },
}

CALENDARS_SCHEMA = {
    "name": "calendar_list_calendars",
    "description": "List available calendars on the configured CalDAV server.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def _not_configured() -> str:
    return (
        "Calendar not configured. Add caldav_url, caldav_username, and caldav_password "
        "to SUNI config (Admin → Settings)."
    )


def list_events_handler(days: int = 7) -> str:
    if not _cal.is_configured():
        return _not_configured()
    events = _cal.get_events(days_ahead=days)
    if not events:
        return f"No events in the next {days} day(s)."
    lines = []
    for ev in events:
        start = ev.get("start", "")[:16].replace("T", " ")
        end   = ev.get("end", "")[:16].replace("T", " ").split(" ", 1)[-1]
        line  = f"{start} → {end}  {ev['summary']}"
        if ev.get("location"):
            line += f"  📍 {ev['location']}"
        lines.append(line)
    return "\n".join(lines)


def create_event_handler(
    summary: str,
    start: str,
    end: str,
    location: str = "",
    description: str = "",
) -> str:
    if not _cal.is_configured():
        return _not_configured()
    try:
        result = _cal.create_event(summary, start, end, location, description)
        return f"Event created: {result['summary']} on {result['start'][:10]} (uid: {result['uid'][:8]}…)"
    except Exception as e:
        return f"Failed to create event: {e}"


def free_slots_handler(
    date: str,
    duration_minutes: int = 60,
    start_hour: int = 9,
    end_hour: int = 18,
) -> str:
    if not _cal.is_configured():
        return _not_configured()
    slots = _cal.find_free_slots(date, duration_minutes, start_hour, end_hour)
    if not slots:
        return f"No free {duration_minutes}-minute slots found on {date}."
    lines = [f"Free {duration_minutes}-min slots on {date}:"]
    for s in slots:
        start = s["start"][11:16]
        end   = s["end"][11:16]
        lines.append(f"  {start} – {end}")
    return "\n".join(lines)


def delete_event_handler(uid: str) -> str:
    if not _cal.is_configured():
        return _not_configured()
    ok = _cal.delete_event(uid)
    return f"Event deleted." if ok else f"Event with uid '{uid}' not found."


def list_calendars_handler() -> str:
    if not _cal.is_configured():
        return _not_configured()
    try:
        names = _cal.list_calendars()
        return "Calendars:\n" + "\n".join(f"  • {n}" for n in names)
    except Exception as e:
        return f"Failed to list calendars: {e}"
