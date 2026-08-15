"""
Discord bot integration for SUNI — Gateway (WebSocket) receive + REST send.

Like the Telegram long-poll gateway, this connects OUT to Discord's Gateway, so
it works from a local machine with NO public URL. Hand-rolled minimal client on
`websockets` + `httpx` (no discord.py dependency — matches SUNI's channel style).

Scope note: RESUME is deliberately NOT implemented — on any disconnect we simply
re-IDENTIFY with exponential backoff. For a personal/SMB bot, missing a handful
of messages across a reconnect is acceptable, and it deletes a whole bug class.

Config (admin panel, preferred) / environment (fallback):
  discord_bot_token / DISCORD_BOT_TOKEN — bot token from the Discord Developer Portal

REQUIRED: the privileged **Message Content Intent** must be toggled ON in the
Developer Portal (Bot → Privileged Gateway Intents). If it is requested in the
IDENTIFY bitfield but OFF in the portal, the gateway closes with code 4014 and
we stop (it is a fatal, non-retryable close).
"""
from __future__ import annotations
import os
import re
import json
import asyncio
import random
import logging
import httpx

log = logging.getLogger("suni.discord")

# Env is a fallback; the live token is resolved at call-time via _token() so the
# admin panel (config `discord_bot_token`) can set/change it.
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

_API     = "https://discord.com/api/v10"
_GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"

# Intents bitfield: GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT
_INTENTS = (1 << 9) | (1 << 12) | (1 << 15)   # 512 | 4096 | 32768 = 37376

# Gateway close codes that are permanent — never reconnect on these, or we storm
# Discord and get rate-limited. 4004 bad token / 4013 invalid intents / 4014
# disallowed (privileged) intents.
_FATAL_CLOSE = {4004, 4013, 4014}

_DISCORD_MSG_LIMIT = 2000   # Discord hard cap per message (Telegram was 4096)


def _token() -> str:
    """Live bot token — config first (admin-settable), env var as fallback."""
    try:
        from .. import config as _cfg
        tok = str(_cfg.get("discord_bot_token", "") or "").strip()
        if tok:
            return tok
    except Exception:
        pass
    return BOT_TOKEN


def is_configured() -> bool:
    return bool(_token())


def _split(text: str, limit: int = _DISCORD_MSG_LIMIT) -> list[str]:
    """Split at sentence/newline boundaries within Discord's 2000-char limit."""
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for piece in re.split(r'(?<=[.!?\n])\s+', text):
        if len(buf) + len(piece) + 1 > limit:
            if buf:
                parts.append(buf.strip())
            # a single oversized piece — hard-chop it
            while len(piece) > limit:
                parts.append(piece[:limit])
                piece = piece[limit:]
            buf = piece
        else:
            buf += (" " if buf else "") + piece
    if buf:
        parts.append(buf.strip())
    return parts or [text[:limit]]


async def send_reply(channel_id: int | str, text: str) -> None:
    """Send a message to a Discord channel via REST. Discord renders markdown
    natively, so the text is sent mostly as-is (no HTML conversion)."""
    tok = _token()
    if not tok:
        log.warning("[DISCORD] not configured — bot token missing")
        return
    headers = {"Authorization": f"Bot {tok}"}   # literal "Bot " prefix is required
    url = f"{_API}/channels/{channel_id}/messages"
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in _split(text):
            try:
                r = await client.post(url, headers=headers, json={"content": chunk})
                if r.status_code == 429:   # rate-limited — honour retry_after once
                    try:
                        retry = float((r.json() or {}).get("retry_after", 1))
                    except Exception:
                        retry = 1.0
                    await asyncio.sleep(min(retry, 5))
                    await client.post(url, headers=headers, json={"content": chunk})
                elif r.status_code >= 400:
                    log.error("[DISCORD] sendMessage %s for channel %s: %s",
                              r.status_code, channel_id, r.text[:200])
            except Exception as e:
                log.error("[DISCORD] sendMessage failed for channel %s: %s", channel_id, e)


async def _heartbeat(ws, interval: float, last_seq: dict) -> None:
    """Discord op-1 heartbeat loop — separate from any WS-level ping. Jitter the
    first beat per Discord guidance. Cancelled by the session on disconnect."""
    await asyncio.sleep(interval * random.random())
    while True:
        await ws.send(json.dumps({"op": 1, "d": last_seq["s"]}))
        await asyncio.sleep(interval)


async def _on_message(d: dict, bot_id: str, dispatch) -> None:
    """Handle a MESSAGE_CREATE dispatch → hand (channel_id, content, is_dm) to
    the server's allow-list gate. Drops bot/self messages (loop prevention)."""
    author    = d.get("author") or {}
    author_id = str(author.get("id") or "")
    if author.get("bot") or (bot_id and author_id == bot_id):
        return   # ignore other bots AND our own messages — else replies loop
    channel_id = str(d.get("channel_id") or "")
    content    = (d.get("content") or "").strip()
    if not channel_id or not content:
        return
    is_dm = d.get("guild_id") is None   # DMs have no guild_id
    await dispatch(channel_id, content, is_dm)


async def _gateway_session(dispatch, stop_event: asyncio.Event) -> str:
    """One gateway connection. Returns 'fatal' (never reconnect), 'reconnect',
    or 'closed'. Re-IDENTIFYs each time (no RESUME by design)."""
    import websockets
    tok = _token()
    try:
        # ping_interval=None: disable the library's WS-level ping; Discord has its
        # own op-1 heartbeat, and library pings can trigger spurious closes.
        async with websockets.connect(_GATEWAY, max_size=2**20,
                                      open_timeout=20, ping_interval=None) as ws:
            hello = json.loads(await ws.recv())
            if hello.get("op") != 10:
                log.warning("[DISCORD] expected HELLO, got op %s", hello.get("op"))
                return "reconnect"
            hb_interval = float(hello["d"]["heartbeat_interval"]) / 1000.0
            last_seq = {"s": None}
            hb_task = asyncio.create_task(_heartbeat(ws, hb_interval, last_seq))
            try:
                await ws.send(json.dumps({"op": 2, "d": {
                    "token":   tok,
                    "intents": _INTENTS,
                    "properties": {"os": "linux", "browser": "suni", "device": "suni"},
                }}))
                bot_id = {"id": None}
                while not stop_event.is_set():
                    msg = json.loads(await ws.recv())
                    if msg.get("s") is not None:
                        last_seq["s"] = msg["s"]
                    op = msg.get("op")
                    if op == 0:   # dispatch event
                        t, d = msg.get("t"), (msg.get("d") or {})
                        if t == "READY":
                            bot_id["id"] = str(((d.get("user") or {}).get("id")) or "")
                            log.info("[DISCORD] gateway ready as %s (id=%s)",
                                     (d.get("user") or {}).get("username"), bot_id["id"])
                        elif t == "MESSAGE_CREATE":
                            await _on_message(d, bot_id["id"], dispatch)
                    elif op == 1:   # server asks for an immediate heartbeat
                        await ws.send(json.dumps({"op": 1, "d": last_seq["s"]}))
                    elif op == 7:   # server asks us to reconnect
                        log.info("[DISCORD] gateway requested reconnect")
                        return "reconnect"
                    elif op == 9:   # invalid session
                        log.info("[DISCORD] invalid session — re-identifying")
                        await asyncio.sleep(1 + random.random() * 2)
                        return "reconnect"
                    # op 11 = heartbeat ACK — ignore (zombie detection not needed;
                    # Discord closes dead connections and we reconnect)
                return "closed"
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except BaseException:
                    pass
    except websockets.ConnectionClosed as e:
        code = getattr(e, "code", None)
        if code in _FATAL_CLOSE:
            log.error("[DISCORD] FATAL gateway close %s — not reconnecting. "
                      "Check bot token and that the Message Content Intent is ON "
                      "in the Developer Portal.", code)
            return "fatal"
        log.warning("[DISCORD] gateway closed (code=%s) — will reconnect", code)
        return "reconnect"


async def run_gateway(dispatch, stop_event: asyncio.Event) -> None:
    """Maintain the Discord gateway connection until stop_event. Reconnects with
    exponential backoff; stops permanently on a fatal close code."""
    if not is_configured():
        log.info("[DISCORD] gateway not started — no bot token configured")
        return

    log.info("[DISCORD] gateway starting")
    backoff = 1
    while not stop_event.is_set():
        sess    = asyncio.create_task(_gateway_session(dispatch, stop_event))
        stopper = asyncio.create_task(stop_event.wait())
        done, _ = await asyncio.wait({sess, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if stopper in done:                 # shutdown — cancel the live session
            sess.cancel()
            try:
                await sess
            except BaseException:
                pass
            break
        stopper.cancel()
        try:
            result = sess.result()
        except Exception as e:
            log.warning("[DISCORD] session error: %s", e)
            result = "error"

        if result == "fatal":
            break                            # permanent failure — do not storm
        # Clean close/reconnect → quick retry; errors → exponential backoff.
        if result in ("closed", "reconnect"):
            delay, backoff = 1, 1
        else:
            delay = backoff
            backoff = min(backoff * 2, 60)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
            break
        except asyncio.TimeoutError:
            pass

    log.info("[DISCORD] gateway stopped")
