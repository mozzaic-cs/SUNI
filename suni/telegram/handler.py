"""
Telegram bot integration for SUNI.

Each chat_id gets its own conversation Context so history is preserved
per user across messages.

Two inbound transports (mutually exclusive — Telegram forbids both at once):
  • WEBHOOK  — server.py /telegram endpoint; needs a public HTTPS URL Telegram
    can reach (SUNI_PUBLIC_URL). Suited to a publicly-hosted deployment.
  • LONG-POLL — poll_updates() below; the server calls OUT to Telegram, so it
    works from a local machine behind NAT with NO public exposure. This is the
    default for a local-first box. Enabled via config `telegram_enabled`.

Config (admin panel, preferred) / environment (fallback):
  telegram_bot_token  / TELEGRAM_BOT_TOKEN    — from @BotFather
  SECRET_TOKEN        / TELEGRAM_SECRET_TOKEN — webhook-only inbound validation
  SUNI_PUBLIC_URL     — public URL for webhook registration info
"""
from __future__ import annotations
import os
import re
import asyncio
import logging
import httpx

log = logging.getLogger("suni.telegram")

# Env is a fallback; the live token is resolved at call-time via _token() so the
# admin panel (config `telegram_bot_token`) can set/change it without a restart.
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SECRET_TOKEN = os.environ.get("TELEGRAM_SECRET_TOKEN", "")

_API = "https://api.telegram.org/bot{token}/{method}"


def _token() -> str:
    """Live bot token — config first (admin-settable), env var as fallback."""
    try:
        from .. import config as _cfg
        tok = str(_cfg.get("telegram_bot_token", "") or "").strip()
        if tok:
            return tok
    except Exception:
        pass
    return BOT_TOKEN


def is_configured() -> bool:
    return bool(_token())


def _api(method: str) -> str:
    return _API.format(token=_token(), method=method)


def _md_to_tg(text: str) -> str:
    """Convert markdown to Telegram HTML (parse_mode=HTML)."""
    # Code blocks before anything else — preserve their content verbatim
    text = re.sub(r'```(?:\w+\n)?(.*?)```', r'<pre>\1</pre>', text, flags=re.DOTALL)
    text = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', text)
    # Bold / italic
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'\*(.+?)\*',     r'<i>\1</i>', text)
    text = re.sub(r'__(.+?)__',     r'<i>\1</i>', text)
    # Headers → bold
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    return text.strip()


def _split(text: str, limit: int = 4000) -> list[str]:
    """Split at sentence boundaries within Telegram's 4096-char limit."""
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for sentence in re.split(r'(?<=[.!?])\s+', text):
        if len(buf) + len(sentence) + 1 > limit:
            if buf:
                parts.append(buf.strip())
            buf = sentence
        else:
            buf += (" " if buf else "") + sentence
    if buf:
        parts.append(buf.strip())
    return parts or [text[:limit]]


async def send_reply(chat_id: int | str, text: str) -> None:
    """Send a Telegram message using HTML parse mode."""
    if not is_configured():
        log.warning("Telegram not configured — BOT_TOKEN missing")
        return
    formatted = _md_to_tg(text)
    chunks    = _split(formatted)
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in chunks:
            try:
                r = await client.post(_api("sendMessage"), json={
                    "chat_id":    chat_id,
                    "text":       chunk,
                    "parse_mode": "HTML",
                })
                if r.status_code != 200:
                    # HTML parse error — retry as plain text
                    await client.post(_api("sendMessage"), json={
                        "chat_id": chat_id,
                        "text":    re.sub(r'<[^>]+>', '', chunk),
                    })
            except Exception as e:
                log.error("[TELEGRAM] sendMessage failed for chat %s: %s", chat_id, e)


# Long-poll read timeout MUST exceed the getUpdates long-poll timeout, or every
# idle poll aborts as a client read-timeout.
_POLL_TIMEOUT   = 25   # seconds Telegram holds the request open waiting for updates
_CLIENT_TIMEOUT = 35   # httpx read timeout — comfortably > _POLL_TIMEOUT


async def poll_updates(dispatch, stop_event: asyncio.Event) -> None:
    """Long-poll getUpdates and hand each inbound (chat_id:int, text:str) to
    `dispatch` (an async callable). Runs until stop_event is set.

    Works from a local machine with no public URL — SUNI calls OUT to Telegram.
    Mutually exclusive with the webhook: we deleteWebhook first, else Telegram
    replies 409 Conflict to every getUpdates and the loop dies.
    """
    if not is_configured():
        log.info("[TELEGRAM] long-poll not started — no bot token configured")
        return

    async with httpx.AsyncClient(timeout=_CLIENT_TIMEOUT) as client:
        # 1) Drop any active webhook so getUpdates is permitted (409 otherwise).
        try:
            await client.post(_api("deleteWebhook"), json={"drop_pending_updates": False})
        except Exception as e:
            log.warning("[TELEGRAM] deleteWebhook failed (continuing): %s", e)

        # 2) Skip any backlog: fetch only the most recent update and advance past
        #    it, so a restart doesn't replay old messages.
        offset = 0
        try:
            r = await client.post(_api("getUpdates"), json={"offset": -1, "timeout": 0})
            res = (r.json() or {}).get("result") or []
            if res:
                offset = res[-1]["update_id"] + 1
        except Exception as e:
            log.warning("[TELEGRAM] initial getUpdates failed (continuing): %s", e)

        log.info("[TELEGRAM] long-poll started (offset=%s)", offset)

        while not stop_event.is_set():
            try:
                # Race the long-poll against stop_event so a live disable/restart
                # takes effect immediately instead of waiting out the 25s poll.
                get_task  = asyncio.ensure_future(client.post(_api("getUpdates"), json={
                    "offset":  offset,
                    "timeout": _POLL_TIMEOUT,
                    "allowed_updates": ["message", "edited_message"],
                }))
                stop_task = asyncio.ensure_future(stop_event.wait())
                done, _ = await asyncio.wait({get_task, stop_task},
                                            return_when=asyncio.FIRST_COMPLETED)
                if stop_task in done:
                    get_task.cancel()
                    try:
                        await get_task
                    except BaseException:
                        pass
                    break
                stop_task.cancel()
                r = get_task.result()
                data = r.json() or {}
                if not data.get("ok"):
                    log.warning("[TELEGRAM] getUpdates not ok: %s", data.get("description"))
                    try:                        # stop-aware backoff
                        await asyncio.wait_for(stop_event.wait(), timeout=5)
                        break
                    except asyncio.TimeoutError:
                        pass
                    continue
                for upd in data.get("result") or []:
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("edited_message")
                    if not msg:
                        continue
                    chat_id = (msg.get("chat") or {}).get("id")
                    text    = (msg.get("text") or "").strip()
                    if chat_id is None or not text:
                        continue
                    try:
                        await dispatch(chat_id, text)
                    except Exception as e:
                        log.error("[TELEGRAM] dispatch failed for chat %s: %s", chat_id, e)
            except httpx.ReadTimeout:
                continue  # normal when no updates arrive within the long-poll window
            except Exception as e:
                log.warning("[TELEGRAM] poll error (retrying): %s", e)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=5)
                    break
                except asyncio.TimeoutError:
                    pass

    log.info("[TELEGRAM] long-poll stopped")
