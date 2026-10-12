"""
SUNI's outbox: nothing leaves SUNI's own mailbox without an admin's approval.

Every send from SUNI's account — the send_email tool, the direct "email this
to …" route, the Claude Code bridge, scheduled deliveries — lands here as a
draft instead of going out. An admin reads it in the admin panel and approves,
edits or rejects it. Enforced at the transport (email_notify.send_email), not
in each caller: four callers patched one by one is how one gets missed.

The one exemption is SUNI's own fixed notices to the configured notification
recipient (email_notify._send): new-article digests, inbox alerts, and the
notice that a draft is waiting here. Their recipient is set by an admin in
config, never chosen by a model, and without them the admin could not be told
that anything needs approving.

Mail from a user's OWN account (set up in their personal settings) is theirs
and is not held here; their own approval card in chat still applies.
"""
from __future__ import annotations
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone

log = logging.getLogger("suni.mail_outbox")

OUTBOX_DB = "memory/mail_outbox.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    requested_by  TEXT NOT NULL DEFAULT '',   -- user id ('' = background job)
    requester     TEXT NOT NULL DEFAULT '',   -- username at request time
    source        TEXT NOT NULL DEFAULT '',   -- tool | direct | schedule | bridge
    to_addr       TEXT NOT NULL,
    subject       TEXT NOT NULL,
    body          TEXT NOT NULL,
    attachments   TEXT NOT NULL DEFAULT '[]', -- JSON list of resolved paths
    in_reply_to   TEXT NOT NULL DEFAULT '',
    refs          TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | rejected | failed
    decided_by    TEXT NOT NULL DEFAULT '',
    decided_at    TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    message_id    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, created_at);
CREATE TABLE IF NOT EXISTS sent_ids (
    message_id    TEXT PRIMARY KEY,
    sent_at       TEXT NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(OUTBOX_DB) or ".", exist_ok=True)
    c = sqlite3.connect(OUTBOX_DB)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["attachments"] = json.loads(d.get("attachments") or "[]")
    except ValueError:
        d["attachments"] = []
    return d


def _username(user_id: str) -> str:
    if not user_id:
        return ""
    try:
        from .. import auth as _auth
        u = _auth.get_user(user_id)
        return u["username"] if u else user_id
    except Exception:          # noqa: BLE001
        return user_id


def _audit(user_id: str, action: str, detail: str, target: str = "") -> None:
    try:
        from .. import audit as _a
        _a.log_event(user_id=user_id, username=_username(user_id) or "system",
                     action=action, detail=detail[:200], target_id=target)
    except Exception:          # noqa: BLE001 — auditing never blocks mail
        log.debug("audit failed for %s", action, exc_info=True)


# ── queue ────────────────────────────────────────────────────────────────────

def queue(to: str, subject: str, body: str, attachments: list[str] | None = None, *,
          requested_by: str = "", source: str = "tool",
          in_reply_to: str = "", refs: str = "") -> str:
    """Hold a message for an admin. Returns the draft id."""
    did = uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute(
            """INSERT INTO outbox (id, created_at, requested_by, requester, source,
                                   to_addr, subject, body, attachments, in_reply_to, refs)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (did, _now(), requested_by or "", _username(requested_by), source,
             to.strip(), subject, body, json.dumps(attachments or []),
             in_reply_to, refs))
    log.info("[OUTBOX] queued %s → %s (%s, by %s)", did, to, source, requested_by or "system")
    _audit(requested_by, "mail.outbox.queued", f"to {to}: {subject}", did)
    _tell_admin(did, to, subject, _username(requested_by), source)
    return did


def _tell_admin(did: str, to: str, subject: str, requester: str, source: str) -> None:
    """A short notice to the notification recipient that a draft is waiting.
    Recipient and subject only — the body is read in the admin panel, behind a
    login, not copied into another mailbox."""
    try:
        from .. import config as _cfg
        if not _cfg.get("mail_outbox_notify", True):
            return
        from .email_notify import _send
        from html import escape
        who = requester or "a scheduled job"
        plain = (f"SUNI has a message waiting for approval.\n\n"
                 f"To:      {to}\nSubject: {subject}\nAsked by: {who} ({source})\n\n"
                 f"Nothing has been sent. Approve or reject it in the admin panel → Mail.")
        html = (f"<p>SUNI has a message waiting for approval.</p>"
                f"<p><b>To:</b> {escape(to)}<br><b>Subject:</b> {escape(subject)}<br>"
                f"<b>Asked by:</b> {escape(who)} ({escape(source)})</p>"
                f"<p>Nothing has been sent. Approve or reject it in the admin panel &rarr; Mail.</p>")
        _send(f"SUNI — approval needed: {subject[:60]}", html, plain)
    except Exception as e:     # noqa: BLE001 — the draft is queued either way
        log.warning("[OUTBOX] could not notify the admin about %s: %s", did, e)


# ── read ─────────────────────────────────────────────────────────────────────

def list_items(status: str = "", limit: int = 200) -> list[dict]:
    with _conn() as c:
        if status:
            rows = c.execute("SELECT * FROM outbox WHERE status=? ORDER BY created_at DESC LIMIT ?",
                             (status, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM outbox ORDER BY (status='pending') DESC, created_at DESC LIMIT ?",
                             (limit,)).fetchall()
    return [_row(r) for r in rows]


def get(did: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM outbox WHERE id=?", (did,)).fetchone()
    return _row(r) if r else None


def pending_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]


def sent_message_ids() -> set[str]:
    with _conn() as c:
        return {r[0] for r in c.execute("SELECT message_id FROM sent_ids").fetchall()}


def record_sent_id(message_id: str) -> None:
    if not message_id:
        return
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO sent_ids (message_id, sent_at) VALUES (?,?)",
                  (message_id, _now()))


# ── decide (admin only — the endpoints check the role) ───────────────────────

def edit(did: str, admin_id: str, *, to: str | None = None, subject: str | None = None,
         body: str | None = None) -> dict:
    d = get(did)
    if not d:
        raise KeyError(did)
    if d["status"] != "pending":
        raise ValueError(f"already {d['status']}")
    with _conn() as c:
        c.execute("UPDATE outbox SET to_addr=?, subject=?, body=? WHERE id=? AND status='pending'",
                  ((to if to is not None else d["to_addr"]).strip(),
                   subject if subject is not None else d["subject"],
                   body if body is not None else d["body"], did))
    _audit(admin_id, "mail.outbox.edited", f"to {to or d['to_addr']}", did)
    return get(did)


def approve(did: str, admin_id: str) -> dict:
    """Send the draft from SUNI's mailbox. Claimed atomically first, so two
    admins clicking at once send it once."""
    with _conn() as c:
        cur = c.execute("UPDATE outbox SET status='sending', decided_by=?, decided_at=? "
                        "WHERE id=? AND status='pending'", (_username(admin_id), _now(), did))
        if cur.rowcount != 1:
            d = get(did)
            if not d:
                raise KeyError(did)
            raise ValueError(f"already {d['status']}")
    d = get(did)
    from .email_notify import deliver_as_suni
    ok, result, mid = deliver_as_suni(
        d["to_addr"], d["subject"], d["body"], d["attachments"],
        in_reply_to=d["in_reply_to"], refs=d["refs"], user_id=d["requested_by"])
    status = "sent" if ok else "failed"
    with _conn() as c:
        c.execute("UPDATE outbox SET status=?, note=?, message_id=? WHERE id=?",
                  (status, result[:300], mid, did))
    if ok:
        record_sent_id(mid)
    _audit(admin_id, f"mail.outbox.{status}", f"to {d['to_addr']}: {result}", did)
    return get(did)


def reject(did: str, admin_id: str, reason: str = "") -> dict:
    with _conn() as c:
        cur = c.execute("UPDATE outbox SET status='rejected', decided_by=?, decided_at=?, note=? "
                        "WHERE id=? AND status='pending'",
                        (_username(admin_id), _now(), reason[:300], did))
        if cur.rowcount != 1:
            d = get(did)
            if not d:
                raise KeyError(did)
            raise ValueError(f"already {d['status']}")
    _audit(admin_id, "mail.outbox.rejected", reason or "", did)
    return get(did)


def retry(did: str, admin_id: str) -> dict:
    """A failed send (bad password, server down) goes back to pending, to be
    approved again once the cause is fixed. So does one stuck in 'sending' for
    over ten minutes: SUNI stopped mid-send, and whether it left is unknown —
    the admin decides, seeing that note."""
    from datetime import timedelta
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    with _conn() as c:
        cur = c.execute("UPDATE outbox SET status='pending', "
                        "note=CASE WHEN status='sending' THEN 'interrupted while sending: it may have gone out' ELSE '' END "
                        "WHERE id=? AND (status='failed' OR (status='sending' AND decided_at < ?))",
                        (did, stale))
        if cur.rowcount != 1:
            raise ValueError("only a failed or interrupted message can be retried")
    _audit(admin_id, "mail.outbox.retry", "", did)
    return get(did)
