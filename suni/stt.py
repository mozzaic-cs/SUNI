"""
Server-side speech-to-text via a self-hosted Whisper endpoint.

Opt-in alternative to the browser's Web Speech API (the default). The browser
path ships microphone audio to Google; the whisper path keeps audio inside your
infrastructure (privacy/enterprise) and works offline. Config `stt_backend`:
'browser' (default, no server involvement) | 'whisper'.

Talks to an OpenAI-compatible audio endpoint (`POST {base}/audio/transcriptions`,
multipart `file` + `model`, returns `{"text": ...}`) — e.g. faster-whisper-server.

Standalone by design: STT and the embedding backend look similar in config only.
They live in different layers, return different things, and don't share an
adapter — so there is deliberately NO shared "capability router" (the config-key
convention `stt_*` / `embed_*` IS the map). The one genuinely shared concern —
remote-endpoint health — is the host-keyed circuit breaker in models/health.py.

NOTE (needs live validation): MediaRecorder emits webm/opus (Chrome) or mp4
(Safari); whisper servers differ in accepted formats. The format round-trip can
only be confirmed against a real whisper server + real microphone.
"""
from __future__ import annotations
import logging

import httpx

from . import config as _cfg
from .models import health as _health

log = logging.getLogger("suni.stt")

_MAX_AUDIO_BYTES = 25 * 1024 * 1024   # 25 MB — matches OpenAI's audio limit
_TIMEOUT = 60


class STTError(Exception):
    """User-facing transcription failure."""


def enabled() -> bool:
    return (str(_cfg.get("stt_backend", "browser")).lower() == "whisper"
            and bool(str(_cfg.get("stt_base_url", "")).strip()))


async def transcribe(audio: bytes, filename: str = "audio.webm",
                     content_type: str = "audio/webm") -> str:
    """Transcribe audio bytes to text via the configured whisper endpoint.
    Raises STTError on any failure (caller should surface it and let the user
    type instead — never hang the mic)."""
    base = str(_cfg.get("stt_base_url", "")).strip()
    if not base:
        raise STTError("Server-side STT is not configured.")
    model   = str(_cfg.get("stt_model", "whisper-1"))
    api_key = str(_cfg.get("stt_api_key", ""))
    host    = base

    # Circuit breaker: fast-fail if the whisper host is known-down.
    if _health.enabled() and not _health.allow(host):
        raise STTError("Transcription backend is temporarily unavailable.")

    url = base.rstrip("/") + "/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                url,
                files={"file": (filename, audio, content_type)},
                data={"model": model},
                headers=headers,
            )
    except Exception as exc:
        # Only connection-level failures move the breaker (a 4xx is request-level).
        if _health.enabled() and _health.is_connection_failure(exc):
            _health.record_failure(host)
        raise STTError(f"Transcription request failed: {exc}")

    if _health.enabled():
        _health.record_success(host)

    if r.status_code != 200:
        log.warning("[STT] whisper endpoint %s returned %s", url, r.status_code)
        raise STTError(f"Transcription failed (HTTP {r.status_code}).")
    try:
        text = (r.json().get("text") or "").strip()
    except Exception:
        # Some servers return text/plain
        text = (r.text or "").strip()
    return text
