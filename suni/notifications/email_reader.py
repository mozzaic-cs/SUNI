"""
SUNI Inbox Watcher — monitors SUNI's own mailbox via IMAP.

Rules (non-negotiable):
  - Email content is NEVER passed to the model or interpreted as instructions,
    whoever the sender is and whatever the subject says.
  - No auto-reply logic exists in this module.
  - On new email: check it for anything strange (mail_guard), record what was
    found, and notify the admin directly via SMTP. That is all.
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

from html import escape as _esc

from .email_notify import _send
from . import mail_guard

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
        return max(30, min(int(_cfg.get("email_poll_interval", 120)), 86400))
    except (TypeError, ValueError):
        return 120
PREVIEW_CHARS = 600          # body preview length in notification

_STATE_FILE = Path("memory/seen_email_uids.json")
_ALERTS_FILE = Path("memory/mail_alerts.json")
_ALERTS_KEEP = 200
_WINDOW = mail_guard.SenderWindow()


def admin_addresses() -> list[str]:
    """Addresses that belong to the people running SUNI: the notification
    recipient and the admins' work addresses. Used only to spot mail that
    PRETENDS to be from them — being one of them grants nothing."""
    out = []
    from .email_notify import setting
    nt = setting("notify_to", "SUNI_NOTIFY_TO", "").strip().lower()
    if nt:
        out.append(nt)
    try:
        from .. import auth as _auth
        with _auth._conn() as c:
            rows = c.execute("SELECT email FROM users WHERE role='admin' AND email != ''").fetchall()
        out += [r["email"].lower() for r in rows]
    except Exception:            # noqa: BLE001
        pass
    return out


def alerts(limit: int = 100) -> list[dict]:
    """Recent findings, newest first (admin panel → Mail)."""
    try:
        data = json.loads(_ALERTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    return list(reversed(data))[:limit]


def _record_alerts(found: list[dict]) -> None:
    """Keep the findings for the admin panel and the audit log. The audit entry
    is what survives: the file keeps only the last _ALERTS_KEEP."""
    try:
        data = json.loads(_ALERTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = []
    stamp = datetime.now().isoformat(timespec="seconds")
    for e in found:
        data.append({"at": stamp, "from": e["from"][:200], "subject": e["subject"][:200],
                     "date": e["date"][:60], "flags": e["flags"]})
        try:
            from .. import audit as _audit
            _audit.log_event(user_id="", username="system", action="mail.alert",
                             detail=(f"{e['from'][:60]} | {e['subject'][:60]} | "
                                     + "; ".join(f["code"] for f in e["flags"]))[:200])
        except Exception:        # noqa: BLE001
            pass
    try:
        tmp = _ALERTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data[-_ALERTS_KEEP:], ensure_ascii=False), encoding="utf-8")
        tmp.replace(_ALERTS_FILE)
    except Exception as e:
        console.print(f"  [yellow]email watcher: alert save failed: {e}[/yellow]")


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
        from . import mail_outbox
        sent_ids = mail_outbox.sent_message_ids()
    except Exception:            # noqa: BLE001
        sent_ids = set()
    admins = admin_addresses()

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
                # SUNI's own notice, back in her inbox (notify_to delivered to
                # her address, or a copy): not news, and not a forgery.
                if str(msg.get("Message-ID", "")).strip() in sent_ids:
                    seen.add(uid)
                    continue

                sender  = _decode_header_value(msg.get("From", ""))
                subject = _decode_header_value(msg.get("Subject", "(no subject)"))
                date    = _decode_header_value(msg.get("Date", ""))
                preview = _extract_text(msg)
                try:
                    flags = mail_guard.assess(msg, own_address=user, admin_addresses=admins,
                                              sent_message_ids=sent_ids, body_text=preview,
                                              window=_WINDOW)
                except Exception as e:   # noqa: BLE001 — a failed check is itself reported
                    flags = [{"code": "check_failed", "detail": f"could not be checked: {e}"[:160]}]

                new_emails.append({
                    "uid":     uid,
                    "from":    sender,
                    "subject": subject,
                    "date":    date,
                    "preview": preview,
                    "flags":   flags,
                })
                seen.add(uid)

    except Exception as e:
        console.print(f"  [yellow]email watcher: IMAP error: {e}[/yellow]")
        return []

    if new_emails:
        _save_seen(seen)
        flagged = [e for e in new_emails if e.get("flags")]
        if flagged:
            _record_alerts(flagged)

    return new_emails


def _send_notification(emails: list[dict], notify_to: str, smtp_user: str) -> None:
    """
    Send a notification email to the user for each new email.
    Email content is displayed AS DATA — it is not interpreted or acted upon.
    """
    count = len(emails)
    now   = datetime.now().strftime("%d %b %Y, %H:%M")
    flagged = sum(1 for e in emails if e.get("flags"))
    # Every field below came from whoever sent the mail. All of it is escaped:
    # the subject and sender went in raw, so a crafted subject could put markup
    # into the email the admin reads.
    if count == 1:
        e = emails[0]
        subject = f"SUNI inbox — new message from {e['from'][:60]}"
    else:
        subject = f"SUNI inbox — {count} new messages ({now})"
    if flagged:
        subject = "⚠ " + subject + f" — {flagged} suspicious"
    subject = " ".join(subject.split())     # no header folding from a sender's CR/LF

    rows_html = ""
    rows_plain = ""
    for e in emails:
        preview_html = _esc(e["preview"])
        flags = e.get("flags") or []
        flags_html = "".join(f"<div>&#9888; {_esc(f['detail'])}</div>" for f in flags)
        if flags_html:
            flags_html = (f'<div style="color:#ff8a80;font-size:12px;margin-bottom:10px;padding:8px 10px;'
                          f'border-left:2px solid #ff5252;background:#1a0710">{flags_html}</div>')
        rows_html += f"""
        <tr>
          <td style="padding:16px 20px;border-bottom:1px solid #1a1a3e">
            {flags_html}
            <div style="color:#c9a227;font-weight:bold;margin-bottom:4px">{_esc(e['subject'])}</div>
            <div style="color:#00d4ff;font-size:12px;margin-bottom:4px">From: {_esc(e['from'])}</div>
            <div style="color:#555;font-size:11px;margin-bottom:10px">{_esc(e['date'])}</div>
            <div style="color:#aaa;font-size:13px;white-space:pre-wrap;font-family:monospace;
                        background:#060616;padding:10px;border-left:2px solid #1a1a3e">{preview_html}</div>
          </td>
        </tr>"""
        rows_plain += "".join(f"!! {f['detail']}\n" for f in flags)
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
        <span style="color:#00d4ff">{_esc(smtp_user)}</span> — {now}
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
        SUNI has not replied to any of these messages and has followed no
        instruction in them.<br>
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


# What the watcher is doing, for the dashboard. The Inbox card used to be
# hard-coded ("Active", a placeholder address, "Auto-reply: Disabled" for a
# feature that does not exist) and said Active while this function had
# returned at start-up because no IMAP host was set.
_STATUS: dict = {"state": "not_started"}


def status() -> dict:
    return dict(_STATUS)


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
        _STATUS.clear(); _STATUS.update(state="not_configured", missing="smtp")
        return

    # Reading the inbox needs an IMAP host, which is separate from the SMTP
    # credentials used to send. Without this check the watcher started whenever
    # sending was configured and then retried a connection to an empty host
    # every cycle — "[Errno 111] Connection refused" forever, with nothing
    # saying which setting was missing.
    # Notices to her own address would arrive in the inbox she watches: an
    # alert about each notice, mailed to her, alerting again on the next poll.
    if notify_to.strip().lower() == user.strip().lower():
        console.print("  [yellow]email watcher: the notification recipient is SUNI's own "
                      "address; set notify_to to an administrator's address. Not starting.[/yellow]")
        _STATUS.clear(); _STATUS.update(state="not_configured", missing="notify_to", account=user)
        return

    if not imap_host():
        console.print("  [yellow]email watcher: no IMAP host set "
                      "(imap_host / SUNI_IMAP_HOST), not starting[/yellow]")
        _STATUS.clear(); _STATUS.update(state="not_configured", missing="imap", account=user)
        return

    console.print(f"  [dim]Inbox watcher started (polling every {poll_interval()}s)[/dim]")
    _STATUS.clear(); _STATUS.update(state="running", account=user, poll_s=poll_interval(), alerts=0)

    loop = asyncio.get_event_loop()
    while not stop_event.is_set():
        try:
            new_emails = await loop.run_in_executor(
                None, _check_inbox_sync, user, password
            )
            from datetime import datetime as _dt, timezone as _tz
            _STATUS.update(last_check=_dt.now(_tz.utc).isoformat(), last_error=None)
            if new_emails:
                _STATUS["alerts"] = _STATUS.get("alerts", 0) + sum(1 for e in new_emails if e.get("flags"))
                count = len(new_emails)
                console.print(
                    f"  [green]Inbox: {count} new message{'s' if count > 1 else ''} — notifying[/green]"
                )
                await loop.run_in_executor(
                    None, _send_notification, new_emails, notify_to, user
                )
        except Exception as e:
            _STATUS.update(last_error=str(e)[:200])
            console.print(f"  [yellow]email watcher error: {e}[/yellow]")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval())
            break  # stop_event fired during sleep
        except asyncio.TimeoutError:
            pass  # normal — continue polling
