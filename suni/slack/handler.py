"""
Slack bot integration for SUNI — Socket Mode (WebSocket) receive + Web API send.

Like the Telegram and Discord gateways, Socket Mode connects OUT to Slack, so it
works from a local machine with NO public URL. Hand-rolled minimal client on
`websockets` + `httpx` (no slack_sdk/bolt dependency — matches SUNI's style).

TWO tokens are required (Slack's design):
  • App-Level token (xapp-…) with the `connections:write` scope — opens the
    Socket Mode WebSocket via apps.connections.open.
  • Bot token (xoxb-…) with `chat:write` (+ `*:history`, `message.*` event subs)
    — used by the Web API to send replies.

Config (admin panel, preferred) / environment (fallback):
  slack_app_token / SLACK_APP_TOKEN  — xapp-… (connections:write)
  slack_bot_token / SLACK_BOT_TOKEN  — xoxb-… (chat:write)

Unlike Discord, Slack uses standard WebSocket ping/pong for keepalive (handled by
the `websockets` library), so there is NO manual heartbeat. The one hard rule:
every envelope carrying an `envelope_id` MUST be ACKed within ~3s or Slack retries.
"""
from __future__ import annotations
import os
import re
import json
import asyncio
import random
import logging
import httpx

log = logging.getLogger("suni.slack")

# Env is a fallback; live tokens resolved at call-time so the admin panel can set them.
APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")   # xapp-…
BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")   # xoxb-…

_API = "https://slack.com/api"

# apps.connections.open errors that are permanent — never retry (or we storm Slack).
_FATAL_OPEN_ERRORS = {
    "invalid_auth", "not_authed", "account_inactive", "token_revoked",
    "token_expired", "no_permission", "missing_scope", "not_allowed_token_type",
}

_SLACK_MSG_LIMIT = 3500   # well under Slack's text cap; split at sentence boundaries


def _token_app() -> str:
    try:
        from .. import config as _cfg
        t = str(_cfg.get("slack_app_token", "") or "").strip()
        if t:
            return t
    except Exception:
        pass
    return APP_TOKEN


def _token_bot() -> str:
    try:
        from .. import config as _cfg
        t = str(_cfg.get("slack_bot_token", "") or "").strip()
        if t:
            return t
    except Exception:
        pass
    return BOT_TOKEN


def is_configured() -> bool:
    """Both tokens are required — app token opens the socket, bot token sends."""
    return bool(_token_app() and _token_bot())


def _mrkdwn(text: str) -> str:
    """Convert common markdown to Slack mrkdwn. Slack uses *bold* (single star)
    and <url|label> links; code fences / inline code pass through unchanged."""
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text, flags=re.DOTALL)        # **bold** → *bold*
    text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)   # headers → bold
    text = re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)', r'<\2|\1>', text)   # [t](url) → <url|t>
    return text


def _split(text: str, limit: int = _SLACK_MSG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for piece in re.split(r'(?<=[.!?\n])\s+', text):
        if len(buf) + len(piece) + 1 > limit:
            if buf:
                parts.append(buf.strip())
            while len(piece) > limit:
                parts.append(piece[:limit])
                piece = piece[limit:]
            buf = piece
        else:
            buf += (" " if buf else "") + piece
    if buf:
        parts.append(buf.strip())
    return parts or [text[:limit]]


async def send_reply(channel: str, text: str) -> None:
    """Send a message to a Slack channel via chat.postMessage (Web API)."""
    tok = _token_bot()
    if not tok:
        log.warning("[SLACK] not configured — bot token missing")
        return
    headers = {"Authorization": f"Bearer {tok}"}
    url = f"{_API}/chat.postMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in _split(_mrkdwn(text)):
            try:
                r = await client.post(url, headers=headers,
                                      json={"channel": channel, "text": chunk})
                data = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
                if not data.get("ok"):
                    log.error("[SLACK] chat.postMessage failed for %s: %s",
                              channel, data.get("error") or r.text[:200])
            except Exception as e:
                log.error("[SLACK] chat.postMessage error for %s: %s", channel, e)


async def _open_connection(client: httpx.AsyncClient) -> tuple[str | None, str]:
    """Call apps.connections.open → (wss_url, status). status ∈ {'ok','fatal','retry'}."""
    tok = _token_app()
    try:
        r = await client.post(f"{_API}/apps.connections.open",
                              headers={"Authorization": f"Bearer {tok}"})
        data = r.json()
    except Exception as e:
        log.warning("[SLACK] apps.connections.open error (will retry): %s", e)
        return None, "retry"
    if data.get("ok"):
        return data.get("url"), "ok"
    err = data.get("error", "")
    if err in _FATAL_OPEN_ERRORS:
        log.error("[SLACK] FATAL apps.connections.open error '%s' — not reconnecting. "
                  "Check the app-level token (xapp-…) and its connections:write scope.", err)
        return None, "fatal"
    log.warning("[SLACK] apps.connections.open error '%s' — will retry", err)
    return None, "retry"


async def _on_event(payload: dict, dispatch) -> None:
    """Handle an events_api payload → hand (channel, text, is_dm) to the gate.
    Drops bot/self and edited/deleted messages (loop prevention)."""
    event = payload.get("event") or {}
    if event.get("type") != "message":
        return
    # Skip our own + other bots' messages, and edit/delete/join subtypes.
    if event.get("bot_id") or event.get("subtype"):
        return
    channel = str(event.get("channel") or "")
    text    = (event.get("text") or "").strip()
    if not channel or not text:
        return
    # DM channels are ids starting with 'D' (or channel_type 'im').
    is_dm = event.get("channel_type") == "im" or channel.startswith("D")
    await dispatch(channel, text, is_dm)


async def _socket_session(url: str, dispatch, stop_event: asyncio.Event) -> str:
    """One Socket Mode connection. Returns 'reconnect' or 'closed'. Slack sends a
    'disconnect' frame before graceful refreshes; we then re-open a fresh URL."""
    import websockets
    # Keep the library's default WS ping/pong for keepalive (Slack expects it).
    async with websockets.connect(url, max_size=2**20, open_timeout=20) as ws:
        while not stop_event.is_set():
            msg = json.loads(await ws.recv())
            mtype = msg.get("type")
            # ACK any enveloped frame IMMEDIATELY (Slack retries if not ACKed in ~3s).
            env_id = msg.get("envelope_id")
            if env_id:
                try:
                    await ws.send(json.dumps({"envelope_id": env_id}))
                except Exception as e:
                    log.warning("[SLACK] ack send failed: %s", e)
            if mtype == "hello":
                log.info("[SLACK] socket mode connected (%s)",
                         (msg.get("connection_info") or {}).get("app_id", ""))
            elif mtype == "disconnect":
                log.info("[SLACK] disconnect frame (reason=%s) — reconnecting",
                         msg.get("reason"))
                return "reconnect"
            elif mtype == "events_api":
                try:
                    await _on_event(msg.get("payload") or {}, dispatch)
                except Exception as e:
                    log.error("[SLACK] event handling failed: %s", e)
            # slash_commands / interactive are ACKed above but otherwise ignored.
        return "closed"


async def run_gateway(dispatch, stop_event: asyncio.Event) -> None:
    """Maintain the Slack Socket Mode connection until stop_event. Re-opens a
    fresh WSS URL on each (re)connect; stops on a fatal auth error."""
    if not is_configured():
        log.info("[SLACK] socket mode not started — app and/or bot token missing")
        return

    log.info("[SLACK] socket mode starting")
    backoff = 1
    async with httpx.AsyncClient(timeout=20) as client:
        while not stop_event.is_set():
            url, status = await _open_connection(client)
            if status == "fatal":
                break
            if status != "ok" or not url:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                    break
                except asyncio.TimeoutError:
                    backoff = min(backoff * 2, 60)
                    continue

            sess    = asyncio.create_task(_socket_session(url, dispatch, stop_event))
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
                log.warning("[SLACK] session error (will reconnect): %s", e)
                result = "error"

            # Graceful disconnect/clean close → immediate reconnect; errors → backoff.
            if result in ("reconnect", "closed"):
                backoff = 1
                delay = 0.5 + random.random()
            else:
                delay = backoff
                backoff = min(backoff * 2, 60)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass

    log.info("[SLACK] socket mode stopped")
