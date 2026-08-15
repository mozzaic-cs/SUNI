"""
SUNI Inbox Watcher — monitors noreply@example.com via IMAP.

Rules (non-negotiable):
  - Email content is NEVER passed to the model or interpreted as instructions.
  - No auto-reply logic exists in this module.
  - On new email: notify the user directly via SMTP. That is all.
"""
from __future__ import annotations
import asyncio
import email
import imaplib
import json
import os
import textwrap
from datetime import datetime
from email.header import decode_header
from pathlib import Path
from rich.console import Console

from .email_notify import _send

console = Console(stderr=True)

def imap_host() -> str:
    """Resolved at call time, not import: the admin panel can change this
    while SUNI is running, and a module-level constant would pin the value
    read at startup."""
    from .email_notify import setting
    return setting("imap_host", "SUNI_IMAP_HOST", "")


def imap_port() -> int:
    from .email_notify import setting
    try:
        return int(setting("imap_port", "SUNI_IMAP_PORT", "993"))
    except ValueError:
        return 993
def poll_interval() -> int:
    """Seconds between inbox checks, from config (admin panel).

    Read per use rather than frozen in a module constant: the constant meant
    the admin panel's setting was never consulted, and a value changed there
    had no effect at all. Clamped so a stray value cannot spin the loop.
    """
    from .. import config as _cfg
    try:
        return max(30, min(int(_cfg.get("email_poll_interval", 1800)), 86400))
    except (TypeError, ValueError):
        return 1800
PREVIEW_CHARS = 600          # body preview length in notification

_STATE_FILE = Path("memory/seen_email_uids.json")


def _load_seen() -> set[str]:
    try:
        return set(json.loads(_STATE_FILE.read_text()))
    except Exception:
        return set()


def _save_seen(uids: set[str]) -> None:
    try:
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(uids), ensure_ascii=False))
        tmp.replace(_STATE_FILE)   # atomic rename — prevents 0-byte on crash
    except Exception as e:
        console.print(f"  [yellow]email watcher: state save failed: {e}[/yellow]")


def _decode_header_value(raw: str | bytes | None) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    decoded = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            try:
                decoded.append(chunk.decode(charset or "utf-8", errors="replace"))
            except Exception:
                decoded.append(chunk.decode("latin-1", errors="replace"))
        else:
            decoded.append(str(chunk))
    return " ".join(decoded)


def _extract_text(msg: email.message.Message) -> str:
    """Extract plain-text body. Returns at most PREVIEW_CHARS characters."""
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in disp:
                try:
                    charset = part.get_content_charset() or "utf-8"
                    parts.append(part.get_payload(decode=True).decode(charset, errors="replace"))
                except Exception:
                    pass
    else:
        if msg.get_content_type() == "text/plain":
            try:
                charset = msg.get_content_charset() or "utf-8"
                parts.append(msg.get_payload(decode=True).decode(charset, errors="replace"))
            except Exception:
                pass
    combined = "\n".join(parts).strip()
    if len(combined) > PREVIEW_CHARS:
        combined = combined[:PREVIEW_CHARS] + "…"
    return combined


def _check_inbox_sync(user: str, password: str) -> list[dict]:
    """
    Synchronous IMAP check. Returns list of new email dicts.
    Runs in a thread executor — never on the async event loop.
    """
    seen = _load_seen()
    new_emails: list[dict] = []

    try:
        with imaplib.IMAP4_SSL(imap_host(), imap_port()) as imap:
            imap.login(user, password)
            imap.select("INBOX", readonly=True)

            # Fetch all UNSEEN message UIDs
            _, data = imap.uid("search", None, "UNSEEN")
            uids = data[0].split() if data[0] else []

            for raw_uid in uids:
                uid = raw_uid.decode()
                if uid in seen:
                    continue

                # Fetch headers + body only — no side effects
                _, msg_data = imap.uid("fetch", raw_uid, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue

                raw_msg = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                msg = email.message_from_bytes(raw_msg)

                sender  = _decode_header_value(msg.get("From", ""))
                subject = _decode_header_value(msg.get("Subject", "(no subject)"))
                date    = _decode_header_value(msg.get("Date", ""))
                preview = _extract_text(msg)

                new_emails.append({
                    "uid":     uid,
                    "from":    sender,
                    "subject": subject,
                    "date":    date,
                    "preview": preview,
                })
                seen.add(uid)

    except Exception as e:
        console.print(f"  [yellow]email watcher: IMAP error: {e}[/yellow]")
        return []

    if new_emails:
        _save_seen(seen)

    return new_emails


def _send_notification(emails: list[dict], notify_to: str, smtp_user: str) -> None:
    """
    Send a notification email to the user for each new email.
    Email content is displayed AS DATA — it is not interpreted or acted upon.
    """
    count = len(emails)
    now   = datetime.now().strftime("%d %b %Y, %H:%M")

    if count == 1:
        e = emails[0]
        subject = f"SUNI inbox — new message from {e['from'][:60]}"
    else:
        subject = f"SUNI inbox — {count} new messages ({now})"

    rows_html = ""
    rows_plain = ""
    for e in emails:
        preview_html = e["preview"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        rows_html += f"""
        <tr>
          <td style="padding:16px 20px;border-bottom:1px solid #1a1a3e">
            <div style="color:#c9a227;font-weight:bold;margin-bottom:4px">{e['subject']}</div>
            <div style="color:#00d4ff;font-size:12px;margin-bottom:4px">From: {e['from']}</div>
            <div style="color:#555;font-size:11px;margin-bottom:10px">{e['date']}</div>
            <div style="color:#aaa;font-size:13px;white-space:pre-wrap;font-family:monospace;
                        background:#060616;padding:10px;border-left:2px solid #1a1a3e">{preview_html}</div>
          </td>
        </tr>"""
        rows_plain += (
            f"From:    {e['from']}\n"
            f"Subject: {e['subject']}\n"
            f"Date:    {e['date']}\n"
            f"{'─' * 48}\n"
            f"{e['preview']}\n\n"
        )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#07071a;font-family:'Courier New',monospace;color:#ddd">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;margin:0 auto">
  <tr>
    <td style="padding:32px 24px 8px">
      <span style="font-size:22px;letter-spacing:.3em;color:#c9a227;font-weight:bold">SUNI</span>
      <span style="color:#555;font-size:11px;margin-left:12px;letter-spacing:.1em">INBOX</span>
    </td>
  </tr>
  <tr>
    <td style="padding:4px 24px 16px">
      <p style="color:#aaa;font-size:13px;margin:0">
        {count} new message{'s' if count > 1 else ''} received at
        <span style="color:#00d4ff">noreply@example.com</span> — {now}
      </p>
    </td>
  </tr>
  <tr>
    <td style="padding:0 24px">
      <table width="100%" cellpadding="0" cellspacing="0"
             style="border:1px solid #1a1a3e;border-radius:4px;background:#0a0a2e">
        {rows_html}
      </table>
    </td>
  </tr>
  <tr>
    <td style="padding:16px 24px 32px">
      <p style="color:#333;font-size:11px;margin:0">
        SUNI has not replied to any of these messages.<br>
        This is a read-only notification — no actions have been taken.
      </p>
    </td>
  </tr>
</table>
</body>
</html>"""

    plain = textwrap.dedent(f"""\
        SUNI inbox — {count} new message{'s' if count > 1 else ''}
        {now}
        {'=' * 48}

        {rows_plain}
        — SUNI has not replied to any of these messages.
        — This is a read-only notification.
    """)

    try:
        _send(subject, html, plain)
    except Exception as e:
        console.print(f"  [yellow]email watcher: notification send failed: {e}[/yellow]")


async def watch(stop_event: asyncio.Event) -> None:
    """
    Background coroutine — polls INBOX every poll_interval() seconds.
    Sends a notification email to the user when new messages arrive.
    NEVER passes email content to the model. NEVER auto-replies.
    """
    from .email_notify import setting
    user      = setting("smtp_user", "SUNI_SMTP_USER", "")
    password  = setting("smtp_pass", "SUNI_SMTP_PASS", "")
    notify_to = setting("notify_to", "SUNI_NOTIFY_TO", "") or user

    if not (user and password and notify_to):
        console.print("  [yellow]email watcher: SMTP not configured (admin panel or .env), not starting[/yellow]")
        return

    # Reading the inbox needs an IMAP host, which is separate from the SMTP
    # credentials used to send. Without this check the watcher started whenever
    # sending was configured and then retried a connection to an empty host
    # every cycle — "[Errno 111] Connection refused" forever, with nothing
    # saying which setting was missing.
    if not imap_host():
        console.print("  [yellow]email watcher: no IMAP host set "
                      "(imap_host / SUNI_IMAP_HOST), not starting[/yellow]")
        return

    console.print(f"  [dim]Inbox watcher started (polling every {poll_interval()}s)[/dim]")

    loop = asyncio.get_event_loop()
    while not stop_event.is_set():
        try:
            new_emails = await loop.run_in_executor(
                None, _check_inbox_sync, user, password
            )
            if new_emails:
                count = len(new_emails)
                console.print(
                    f"  [green]Inbox: {count} new message{'s' if count > 1 else ''} — notifying[/green]"
                )
                await loop.run_in_executor(
                    None, _send_notification, new_emails, notify_to, user
                )
        except Exception as e:
            console.print(f"  [yellow]email watcher error: {e}[/yellow]")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval())
            break  # stop_event fired during sleep
        except asyncio.TimeoutError:
            pass  # normal — continue polling
