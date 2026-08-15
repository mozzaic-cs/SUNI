"""
Self-hosted text-to-speech via an OpenAI-compatible speech endpoint.

Opt-in alternative to edge-tts (the default, which sends the reply text to
Microsoft's cloud). The self-hosted path keeps the spoken text inside your
infrastructure (privacy/enterprise) and works offline. Config `tts_backend`:
'edge' (default) | 'server'.

Talks to an OpenAI-compatible speech endpoint (`POST {base}/audio/speech`,
JSON `{model, input, voice, response_format}`, returns raw audio bytes) — e.g.
Kokoro-FastAPI, openai-edge-tts, and similar wrappers.

Self-contained (mirrors stt.py / vision.py); reuses the host-keyed breaker. The
voice string is the existing tts_voice setting (server voice IDs differ from
edge's — set tts_voice to whatever the target server expects).
"""
from __future__ import annotations
import logging

import httpx

from . import config as _cfg
from .models import health as _health

log = logging.getLogger("suni.tts")

_TIMEOUT = 30
_MAX_CHARS = 8000   # guard against pathologically long synthesis requests


class TTSError(Exception):
    """Synthesis failure (caller falls back / returns 503)."""


def enabled() -> bool:
    return (str(_cfg.get("tts_backend", "edge")).lower() == "server"
            and bool(str(_cfg.get("tts_base_url", "")).strip()))


async def synthesize(text: str, voice: str) -> bytes:
    """Return synthesized audio bytes (mp3) for `text` from the configured
    self-hosted TTS server. Raises TTSError on any failure."""
    base = str(_cfg.get("tts_base_url", "")).strip()
    if not base:
        raise TTSError("Self-hosted TTS is not configured.")
    model   = str(_cfg.get("tts_model", "tts-1"))
    api_key = str(_cfg.get("tts_api_key", ""))
    host    = base

    if _health.enabled() and not _health.allow(host):
        raise TTSError("TTS backend is temporarily unavailable.")

    url = base.rstrip("/") + "/audio/speech"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {
        "model": model,
        "input": text[:_MAX_CHARS],
        "voice": voice,
        "response_format": "mp3",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, json=payload, headers=headers)
    except Exception as exc:
        if _health.enabled() and _health.is_connection_failure(exc):
            _health.record_failure(host)
        raise TTSError(f"TTS request failed: {exc}")

    if _health.enabled():
        _health.record_success(host)

    if r.status_code != 200:
        log.warning("[TTS] endpoint %s returned %s", url, r.status_code)
        raise TTSError(f"TTS failed (HTTP {r.status_code}).")

    audio = r.content
    if not audio:
        raise TTSError("TTS returned empty audio.")
    return audio
