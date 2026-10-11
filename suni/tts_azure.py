"""Microsoft's neural voices through the official Azure Speech API.

The same voices edge-tts reaches (pt-PT-RaquelNeural and the rest), but
through the documented, keyed service instead of imitating the Edge
browser's Read Aloud — the route Microsoft began refusing with 403.

Off until AZURE_SPEECH_KEY and AZURE_SPEECH_REGION are set in .env. On the
free tier (F0) an exhausted month is refused, never billed; the caller then
falls back like for any failed voice.
"""
from __future__ import annotations

import logging
import os
from xml.sax.saxutils import escape, quoteattr

import httpx

_log = logging.getLogger("suni.tts_azure")

_FORMAT = "audio-24khz-48kbitrate-mono-mp3"


def _creds() -> tuple[str, str]:
    return (os.environ.get("AZURE_SPEECH_KEY", "").strip(),
            os.environ.get("AZURE_SPEECH_REGION", "").strip().lower())


def configured() -> bool:
    key, region = _creds()
    return bool(key and region)


def _ssml(text: str, voice: str) -> str:
    # The text is escaped: a reply containing "<" or "&" must be read, not
    # parsed as markup (and must not break the request).
    return ("<speak version='1.0' xml:lang=%s><voice name=%s>%s</voice></speak>"
            % (quoteattr(voice[:5]), quoteattr(voice), escape(text)))


async def speak(text: str, voice: str) -> bytes:
    """MP3 for `text` in the neural voice `voice`. Raises on any failure, so
    the caller can start its cooldown and fall back."""
    key, region = _creds()
    if not (key and region):
        raise RuntimeError("Azure Speech is not configured")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
            headers={"Ocp-Apim-Subscription-Key": key,
                     "Content-Type": "application/ssml+xml",
                     "X-Microsoft-OutputFormat": _FORMAT,
                     "User-Agent": "SUNI"},
            content=_ssml(text, voice).encode("utf-8"))
    if r.status_code != 200 or not r.content:
        # Never the key in the log; the status says enough (401 key, 429 quota).
        raise RuntimeError(f"Azure Speech answered {r.status_code}")
    return r.content
