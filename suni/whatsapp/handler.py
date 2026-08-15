"""
WhatsApp integration for SUNI via Twilio.

Each sender gets their own conversation Context so history is preserved
per user across messages.

Environment variables required:
  TWILIO_ACCOUNT_SID   — from console.twilio.com
  TWILIO_AUTH_TOKEN    — from console.twilio.com
  TWILIO_WHATSAPP_FROM — your Twilio WhatsApp number, e.g. whatsapp:+14155238886
                         (sandbox default is +14155238886)
  SUNI_PUBLIC_URL      — public URL where SUNI is reachable, e.g. https://suni.example.com
                         (used only for display/setup info)
"""
from __future__ import annotations
import os
import re
import asyncio
import logging
from twilio.rest import Client

log = logging.getLogger("suni.whatsapp")

ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
FROM_NUMBER = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")


def is_configured() -> bool:
    return bool(ACCOUNT_SID and AUTH_TOKEN)


def _client() -> Client:
    return Client(ACCOUNT_SID, AUTH_TOKEN)


def _md_to_wa(text: str) -> str:
    """Convert markdown to WhatsApp-compatible formatting."""
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)      # bold
    text = re.sub(r'__(.+?)__',     r'_\1_', text)        # italic
    text = re.sub(r'#{1,6}\s+(.+)', r'*\1*', text)        # headers → bold
    text = re.sub(r'`([^`]+)`',     r'`\1`', text)        # inline code (WA supports)
    return text.strip()


def _split(text: str, limit: int = 1500) -> list[str]:
    """Split long responses into chunks at sentence boundaries."""
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


async def send_reply(to: str, text: str) -> None:
    """Send a WhatsApp message via Twilio (async wrapper)."""
    if not is_configured():
        log.warning("Twilio not configured — cannot send WhatsApp reply")
        return
    formatted = _md_to_wa(text)
    chunks    = _split(formatted)
    client    = _client()
    to_wa     = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
    loop      = asyncio.get_event_loop()
    for chunk in chunks:
        await loop.run_in_executor(
            None,
            lambda c=chunk: client.messages.create(
                from_=FROM_NUMBER,
                to=to_wa,
                body=c,
            )
        )
