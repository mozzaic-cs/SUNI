"""
Proactive daily briefing for SUNI.

Composes a morning summary and pushes it via Telegram/WhatsApp.
Also exposes get_briefing() for on-demand use.

Config keys (suni_config):
  briefing_enabled   : bool  (default False — opt-in)
  briefing_time      : "HH:MM" 24h local time (default "08:00")
  briefing_user_id   : user ID whose memory/settings to use
  telegram_notify_chat_id : Telegram chat_id to push to
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("suni.briefing")


# ── Content builders ─────────────────────────────────────────────────────────

def _section_email() -> str:
    """Count unread emails from IMAP if configured."""
    try:
        import imaplib
        from .notifications.email_reader import IMAP_HOST, IMAP_PORT
        if not IMAP_HOST:
            return ""
        import os
        user = os.environ.get("IMAP_USER", "")
        pwd  = os.environ.get("IMAP_PASS", "")
        if not user or not pwd:
            return ""
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as m:
            m.login(user, pwd)
            m.select("INBOX", readonly=True)
            _, data = m.search(None, "UNSEEN")
            count = len(data[0].split()) if data and data[0] else 0
        if count == 0:
            return ""
        return f"📧 **Email**: {count} unread message{'s' if count != 1 else ''} in INBOX."
    except Exception:
        return ""


def _section_contacts() -> str:
    """Contacts whose last_seen was recent — may need follow-up."""
    try:
        from . import contacts as _c
        rows, _ = _c.list_contacts(limit=100)
        today = datetime.now(timezone.utc).date()
        due = []
        for c in rows:
            ls = c.get("last_seen", "")
            if not ls:
                continue
            try:
                last = datetime.fromisoformat(ls.replace("Z", "+00:00")).date()
                age  = (today - last).days
                if 7 <= age <= 30:
                    due.append(f"{c['name']}" + (f" @ {c['company']}" if c.get("company") else ""))
            except Exception:
                pass
        if not due:
            return ""
        sample = ", ".join(due[:5])
        more   = f" (+{len(due) - 5} more)" if len(due) > 5 else ""
        return f"👥 **Follow-up**: {sample}{more} not contacted in 7–30 days."
    except Exception:
        return ""


def _section_monitor() -> str:
    """Unnotified monitor alerts since last briefing."""
    try:
        from . import monitor as _m
        alerts = _m.get_pending_alerts(limit=10)
        if not alerts:
            return ""
        lines = [f"🔔 **Monitor** — {len(alerts)} new alert(s):"]
        for a in alerts[:5]:
            lines.append(f"  • {a['title'][:80]}")
            lines.append(f"    {a['url']}")
        if len(alerts) > 5:
            lines.append(f"  …and {len(alerts) - 5} more.")
        # Mark as notified
        _m.mark_notified([a["id"] for a in alerts])
        return "\n".join(lines)
    except Exception:
        return ""


def _section_kb() -> str:
    """Knowledge base items ingested in the last 24 h."""
    try:
        from .memory.document_store import DocumentStore
        from pathlib import Path
        ds = DocumentStore(Path("memory"))
        stats = ds.stats()
        recent = stats.get("recent_24h", 0)
        if not recent:
            return ""
        return f"📚 **Knowledge base**: {recent} new document(s) ingested in the last 24 h."
    except Exception:
        return ""


async def compose_briefing() -> str:
    """Build the full briefing text."""
    now   = datetime.now(timezone.utc)
    today = now.strftime("%A, %d %B %Y")

    sections = [f"**Good morning — SUNI Daily Briefing for {today}**\n"]

    # Run I/O-bound section builders in a thread pool to avoid blocking
    loop = asyncio.get_event_loop()
    parts = await asyncio.gather(
        loop.run_in_executor(None, _section_email),
        loop.run_in_executor(None, _section_contacts),
        loop.run_in_executor(None, _section_monitor),
        loop.run_in_executor(None, _section_kb),
    )
    for p in parts:
        if p:
            sections.append(p)

    if len(sections) == 1:
        sections.append("Nothing urgent today. Have a productive day.")

    return "\n\n".join(sections)


# ── Push helpers ─────────────────────────────────────────────────────────────

async def push_briefing(text: str) -> list[str]:
    """Send briefing to all configured channels. Returns list of channels used."""
    from . import config as _cfg
    sent = []

    # Telegram
    try:
        from .telegram.handler import send_reply as tg_send, is_configured as tg_ok
        chat_id = _cfg.get("telegram_notify_chat_id", "")
        if tg_ok() and chat_id:
            await tg_send(chat_id, text)
            sent.append("telegram")
    except Exception as e:
        log.warning("[BRIEFING] telegram push failed: %s", e)

    # WhatsApp
    try:
        from .whatsapp.handler import send_reply as wa_send, is_configured as wa_ok
        wa_to = _cfg.get("whatsapp_notify_number", "")
        if wa_ok() and wa_to:
            await wa_send(wa_to, text)
            sent.append("whatsapp")
    except Exception as e:
        log.warning("[BRIEFING] whatsapp push failed: %s", e)

    return sent


# ── Background scheduler ─────────────────────────────────────────────────────

_stop_event: asyncio.Event | None = None


def _seconds_until(target_hhmm: str) -> float:
    """Seconds until the next occurrence of HH:MM local time."""
    try:
        h, m = (int(x) for x in target_hhmm.split(":"))
    except (ValueError, AttributeError):
        h, m = 8, 0
    now = datetime.now()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def start_briefing_scheduler() -> None:
    """Long-running background task that fires the briefing once per day."""
    global _stop_event
    _stop_event = asyncio.Event()

    from . import config as _cfg

    while not _stop_event.is_set():
        if not _cfg.get("briefing_enabled", False):
            # Poll config every 5 minutes when disabled
            try:
                await asyncio.wait_for(_stop_event.wait(), timeout=300)
            except asyncio.TimeoutError:
                pass
            continue

        briefing_time = _cfg.get("briefing_time", "08:00")
        delay = _seconds_until(briefing_time)
        log.info("[BRIEFING] next briefing in %.0fs (%s)", delay, briefing_time)

        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass   # time to fire

        if _stop_event.is_set():
            break

        try:
            text  = await compose_briefing()
            sent  = await push_briefing(text)
            log.info("[BRIEFING] sent via %s", sent or "none")
        except Exception as e:
            log.error("[BRIEFING] failed: %s", e, exc_info=True)


def stop_briefing_scheduler() -> None:
    if _stop_event:
        _stop_event.set()
