"""
Server-side speech-to-text via a self-hosted Whisper endpoint.

Opt-in alternative to the browser's Web Speech API (the default). The browser
path ships microphone audio to Google; the whisper paths keep audio inside your
infrastructure (privacy/enterprise) and work offline. Config `stt_backend`:
'browser' (default, no server involvement) | 'local' | 'whisper'.

'local' uses the faster-whisper already installed for meeting transcription —
no server, no endpoint, nothing to deploy. It was added after the browser path
heard "Quero ver os ficheiros indexados, com um resumo por cliente" as "Nós
estamos ficheiros, OK? Quer dizer, de facto, os incluídos são muito circulos".
Google's free recogniser is markedly weaker on European Portuguese than on
Brazilian, and worse again at any distance from the microphone. On the same
sentence in a natural voice, the local `base` model returned "Quer ver os
ficheres indexados com o resumo por cliente" — imperfect, and a different
category of imperfect.

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


def backend() -> str:
    return str(_cfg.get("stt_backend", "browser")).lower()


def local_available() -> bool:
    """Is the in-process engine usable? Cheap: no model is loaded to answer."""
    try:
        from . import transcription
        return bool(transcription.available())
    except Exception:      # noqa: BLE001
        return False


def enabled() -> bool:
    b = backend()
    if b == "local":
        return local_available()
    return b == "whisper" and bool(str(_cfg.get("stt_base_url", "")).strip())


async def transcribe(audio: bytes, filename: str = "audio.webm",
                     content_type: str = "audio/webm",
                     language: str = "") -> str:
    """Transcribe audio bytes to text via the configured whisper endpoint.
    Raises STTError on any failure (caller should surface it and let the user
    type instead — never hang the mic)."""
    if backend() == "local":
        return await _transcribe_local(audio, filename, language)

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


async def _transcribe_local(audio: bytes, filename: str,
                            language: str = "") -> str:
    """faster-whisper, in this process, on a temporary file.

    A file rather than a buffer because that is what the library takes, and
    because the browser sends webm/opus which ffmpeg (already a dependency of
    meeting recording) decodes on the way in.

    The model choice is the whole quality question. `base` is what meeting
    transcription uses and it is adequate for European Portuguese in a natural
    voice; `small` is noticeably better and about three times slower on a CPU
    without AVX2. `stt_model_local` decides, so nobody has to edit code to
    trade one for the other.
    """
    import os
    import tempfile

    from . import transcription

    if not transcription.available():
        raise STTError("Local transcription is not installed.")
    # THE SPEAKER'S language, passed in by the route from their own settings.
    # Reading the global config here sent Portuguese speech to whisper labelled
    # "en" — because the instance default is en-GB while this user is pt-PT —
    # and whisper faithfully produced English: "You never did that before. Most
    # of them should see the index at."
    #
    # When the speaker has not said, fall back to the instance default. I had
    # written the opposite here — that detection beats a possibly-wrong label —
    # and then measured it: on the same clip, whisper's own detection ALSO
    # chose English and returned "want to see the indexed screws with the
    # client's resume". On a short utterance detection is a guess like any
    # other, so the instance default is the better last resort.
    lang = (language or str(_cfg.get("stt_language", "") or "")).strip()
    # Whisper wants a bare code; the UI speaks BCP-47 ("pt-PT").
    lang = lang.split("-")[0].lower() if lang else ""

    suffix = os.path.splitext(filename)[1] or ".webm"
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="suni_stt_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(audio)
        segments = await transcription.transcribe_file(path, language=lang)
    except Exception as exc:      # noqa: BLE001
        raise STTError(f"Transcription failed: {exc}")
    finally:
        # The recording is somebody speaking. It does not outlive the request.
        try:
            os.unlink(path)
        except OSError:
            pass
    return " ".join((s.get("text") or "").strip() for s in segments).strip()
