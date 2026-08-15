"""
CalDAV calendar integration for SUNI.

Supports Google Calendar, Outlook (via Nextcloud bridge or Exchange CalDAV),
Apple Calendar, Nextcloud, and any standards-compliant CalDAV server.

Config keys (suni_config):
  caldav_url      : CalDAV server URL
  caldav_username : username
  caldav_password : password (stored encrypted via crypto_utils)
  caldav_calendar : calendar display name to use (default: first writable calendar)

Google CalDAV:  https://www.googleapis.com/caldav/v2/  (needs App Password or OAuth)
Apple iCloud:   https://caldav.icloud.com/
Nextcloud:      https://yourserver/remote.php/dav/
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

log = logging.getLogger("suni.calendar")


def _get_config() -> dict:
    try:
        from . import config as _cfg
        return {
            "url":      _cfg.get("caldav_url", ""),
            "username": _cfg.get("caldav_username", ""),
            "password": _cfg.get("caldav_password", ""),
            "calendar": _cfg.get("caldav_calendar", ""),
        }
    except Exception:
        return {}


def is_configured() -> bool:
    c = _get_config()
    return bool(c.get("url") and c.get("username") and c.get("password"))


def _connect():
    import caldav
    c = _get_config()
    client = caldav.DAVClient(
        url=c["url"],
        username=c["username"],
        password=c["password"],
    )
    principal = client.principal()
    cals = principal.calendars()
    if not cals:
        raise RuntimeError("No calendars found on CalDAV server")
    target_name = c.get("calendar", "")
    if target_name:
        cal = next((x for x in cals if x.name == target_name), None)
        if not cal:
            log.warning("[CAL] Calendar %r not found — using first calendar", target_name)
            cal = cals[0]
    else:
        cal = cals[0]
    return cal


def list_calendars() -> list[str]:
    import caldav
    c = _get_config()
    client = caldav.DAVClient(url=c["url"], username=c["username"], password=c["password"])
    return [cal.name for cal in client.principal().calendars()]


def _fmt_event(event) -> dict:
    """Parse a caldav Event into a plain dict."""
    try:
        import vobject
        comp = list(event.icalendar_component.subcomponents) if hasattr(
            event.icalendar_component, "subcomponents"
        ) else []
        vevent = event.vobject_instance.vevent
        summary = str(vevent.summary.value) if hasattr(vevent, "summary") else "(no title)"
        dtstart = vevent.dtstart.value if hasattr(vevent, "dtstart") else None
        dtend   = vevent.dtend.value   if hasattr(vevent, "dtend")   else None
        loc     = str(vevent.location.value) if hasattr(vevent, "location") else ""
        desc    = str(vevent.description.value)[:200] if hasattr(vevent, "description") else ""
        uid     = str(vevent.uid.value) if hasattr(vevent, "uid") else ""

        def _fmt_dt(dt) -> str:
            if dt is None: return ""
            if isinstance(dt, datetime):
                return dt.isoformat()
            return str(dt)

        return {
            "uid":     uid,
            "summary": summary,
            "start":   _fmt_dt(dtstart),
            "end":     _fmt_dt(dtend),
            "location": loc,
            "description": desc,
        }
    except Exception as e:
        log.debug("[CAL] parse error: %s", e)
        return {"uid": "", "summary": str(event), "start": "", "end": "", "location": "", "description": ""}


def get_events(
    days_ahead: int = 7,
    days_behind: int = 0,
) -> list[dict]:
    if not is_configured():
        return []
    try:
        cal   = _connect()
        start = datetime.now(timezone.utc) - timedelta(days=days_behind)
        end   = start + timedelta(days=days_ahead + days_behind)
        events = cal.date_search(start=start, end=end, expand=True)
        result = [_fmt_event(e) for e in events]
        result.sort(key=lambda e: e.get("start", ""))
        return result
    except Exception as e:
        log.error("[CAL] get_events failed: %s", e)
        return []


def create_event(
    summary: str,
    start: str,
    end: str,
    location: str = "",
    description: str = "",
) -> dict:
    """
    Create a calendar event.
    start / end: ISO 8601 strings, e.g. '2026-06-10T10:00:00+01:00'
    """
    import uuid as _uuid
    import vobject
    cal   = _connect()
    uid   = str(_uuid.uuid4())
    vcal  = vobject.iCalendar()
    vevent = vobject.newFromBehavior("vevent")
    vevent.add("uid").value   = uid
    vevent.add("summary").value = summary

    def _parse(s: str) -> datetime:
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)

    vevent.add("dtstart").value = _parse(start)
    vevent.add("dtend").value   = _parse(end)
    if location:
        vevent.add("location").value = location
    if description:
        vevent.add("description").value = description
    vcal.add(vevent)

    cal.save_event(vcal.serialize())
    return {"uid": uid, "summary": summary, "start": start, "end": end}


def delete_event(uid: str) -> bool:
    try:
        cal    = _connect()
        events = cal.search(uid=uid)
        if not events:
            return False
        events[0].delete()
        return True
    except Exception as e:
        log.error("[CAL] delete failed uid=%s: %s", uid, e)
        return False


def find_free_slots(
    date: str,
    duration_minutes: int = 60,
    start_hour: int = 9,
    end_hour: int = 18,
) -> list[dict]:
    """
    Find free time slots on a given date.
    date: 'YYYY-MM-DD'
    Returns list of {start, end} ISO strings.
    """
    from datetime import timezone as _tz
    try:
        day_start = datetime.fromisoformat(f"{date}T{start_hour:02d}:00:00").replace(tzinfo=_tz.utc)
        day_end   = datetime.fromisoformat(f"{date}T{end_hour:02d}:00:00").replace(tzinfo=_tz.utc)
        events    = get_events(
            days_ahead=1,
            days_behind=0,
        )
        # Filter to this day
        busy = []
        for ev in events:
            try:
                s = datetime.fromisoformat(ev["start"].replace("Z", "+00:00"))
                e = datetime.fromisoformat(ev["end"].replace("Z", "+00:00"))
                if s.date().isoformat() == date:
                    busy.append((s, e))
            except Exception:
                pass
        busy.sort()

        slots  = []
        cursor = day_start
        step   = timedelta(minutes=duration_minutes)
        while cursor + step <= day_end:
            slot_end = cursor + step
            overlap  = any(s < slot_end and e > cursor for s, e in busy)
            if not overlap:
                slots.append({"start": cursor.isoformat(), "end": slot_end.isoformat()})
            cursor += timedelta(minutes=30)
        return slots[:10]
    except Exception as e:
        log.error("[CAL] find_free_slots failed: %s", e)
        return []
