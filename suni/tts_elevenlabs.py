"""ElevenLabs voices through the official SDK.

Chosen in the admin panel (tts_backend = "elevenlabs"). The key comes from
ELEVENLABS_API_KEY in .env — `npx -y @elevenlabs/cli@latest onboard init`
writes it there — and is never logged.

Portuguese is only ever spoken by a European Portuguese (pt-PT) voice. The
account's premade voices are English ones that ElevenLabs verified for pt-BR,
and a multilingual voice will happily read Portuguese with a Brazilian or
American accent. When the account has no pt-PT voice, pick() returns None and
the caller speaks with Microsoft instead.

speak() raises on any failure — QuotaExceeded when the month's credits are
spent — so the caller can bench this provider for a while and fall back.
"""
from __future__ import annotations

import logging
import os
import time

from . import config as _cfg

_log = logging.getLogger("suni.tts_elevenlabs")

PREFIX = "eleven:"
DEFAULT_MODEL = "eleven_flash_v2_5"       # ~75 ms; replies are spoken sentence by sentence
MODELS = ("eleven_flash_v2_5", "eleven_multilingual_v2", "eleven_v4")
_NO_LANGUAGE_CODE = {"eleven_multilingual_v2"}   # the API refuses it there
_FORMAT = "mp3_44100_64"
_VOICES_TTL_S = 600
_VOICES_RETRY_S = 60          # after a failed fetch: not one round trip per sentence
_VOICES_TIMEOUT_S = 5
_PT_PT_ACCENTS = {"european", "portugal", "european portuguese", "portuguese (portugal)"}

_voice_cache: dict = {"at": 0.0, "voices": []}


class QuotaExceeded(RuntimeError):
    """The account cannot pay for this: no credits left this period, or a voice
    its plan does not include."""


def _key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()


def configured() -> bool:
    return bool(_key())


_clients: dict = {}


def _client():
    """One client per key: a new one per sentence paid a TLS handshake each
    time and left its connections open."""
    key = _key()
    if key not in _clients:
        from elevenlabs.client import AsyncElevenLabs
        _clients.clear()
        _clients[key] = AsyncElevenLabs(api_key=key, timeout=15)
    return _clients[key]


def _describe(v) -> dict:
    labels = dict(v.labels or {})
    languages, locales = set(), set()
    if labels.get("language"):
        languages.add(str(labels["language"]).lower()[:2])
    for vl in v.verified_languages or []:
        if vl.language:
            languages.add(str(vl.language).lower()[:2])
        if vl.locale:
            locales.add(str(vl.locale).lower())
    if (labels.get("language", "").lower() == "pt"
            and labels.get("accent", "").lower() in _PT_PT_ACCENTS):
        locales.add("pt-pt")
    name = (v.name or v.voice_id).split(" - ")[0].strip()
    tag = "pt-PT" if "pt-pt" in locales else (labels.get("language") or "").lower()
    return {"id": v.voice_id, "name": name, "languages": languages, "locales": locales,
            "label": f"{name} ({tag}) — ElevenLabs" if tag else f"{name} — ElevenLabs"}


async def voices() -> list[dict]:
    """The account's voices, cached for a few minutes. A failed fetch keeps the
    last good list rather than emptying every voice menu."""
    if not configured():
        return []
    now = time.monotonic()
    if _voice_cache["voices"] and now - _voice_cache["at"] < _VOICES_TTL_S:
        return _voice_cache["voices"]
    if now < _voice_cache.get("retry_at", 0.0):
        return _voice_cache["voices"]
    try:
        r = await _client().voices.get_all(
            request_options={"timeout_in_seconds": _VOICES_TIMEOUT_S})
        _voice_cache["voices"] = [_describe(v) for v in r.voices]
        _voice_cache["at"] = now
    except Exception as exc:      # noqa: BLE001
        # Every /api/tts asks for this list. Unreachable or refused, it must
        # not be asked again before every sentence: speech falls back to
        # Microsoft (no voice to pick) and this tries again in a minute.
        _voice_cache["retry_at"] = now + _VOICES_RETRY_S
        _log.warning("[TTS] ElevenLabs voice list failed: %s", _status(exc))
    return _voice_cache["voices"]


def suits(voice: dict, lang: str | None) -> bool:
    """Whether `voice` may speak `lang`. Portuguese means European Portuguese."""
    base = (lang or "").strip().lower()[:2]
    if not base:
        return True
    if base == "pt":
        return "pt-pt" in voice["locales"]
    return base in voice["languages"]


async def pick(saved: str, admin_default: str, lang: str | None) -> str | None:
    """Voice id for this speaker: theirs, then the admin's, then the first one
    that suits their language — each only if it does. None: no voice fits."""
    vs = await voices()
    by_id = {v["id"]: v for v in vs}
    for cand in (saved, admin_default):
        vid = (cand or "").removeprefix(PREFIX)
        if vid in by_id and suits(by_id[vid], lang):
            return vid
    for v in vs:
        if suits(v, lang):
            return v["id"]
    return None


def label_for(voice_id: str) -> str:
    vid = (voice_id or "").removeprefix(PREFIX)
    for v in _voice_cache["voices"]:
        if v["id"] == vid:
            return v["label"]
    return vid


def _status(exc: Exception) -> str:
    code = getattr(exc, "status_code", None)
    return f"HTTP {code}" if code else type(exc).__name__


def _is_quota(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 402:
        return True
    body = str(getattr(exc, "body", "") or "").lower()
    return any(s in body for s in ("quota_exceeded", "insufficient_credits",
                                   "credits", "character_limit"))


async def speak(text: str, voice_id: str, lang: str | None) -> bytes:
    """MP3 for `text`. Raises QuotaExceeded or RuntimeError; never the key in
    the message."""
    if not configured():
        raise RuntimeError("ElevenLabs is not configured")
    model = str(_cfg.get("elevenlabs_model") or DEFAULT_MODEL)
    kwargs = {"text": text, "model_id": model, "output_format": _FORMAT}
    base = (lang or "").strip().lower()[:2]
    if base and model not in _NO_LANGUAGE_CODE:
        kwargs["language_code"] = base
    try:
        buf = bytearray()
        async for chunk in _client().text_to_speech.convert(voice_id.removeprefix(PREFIX), **kwargs):
            buf.extend(chunk)
    except Exception as exc:      # noqa: BLE001
        if _is_quota(exc):
            # Spent credits, or a voice the plan does not cover ("Free users
            # cannot use library voices via the API" is a 402 too): either
            # way it will not change in minutes, so the long rest.
            why = "needs a paid plan" if "paid_plan_required" in str(getattr(exc, "body", ""))                 else "credits exhausted"
            raise QuotaExceeded(f"ElevenLabs {why} ({_status(exc)})") from None
        raise RuntimeError(f"ElevenLabs answered {_status(exc)}") from None
    if not buf:
        raise RuntimeError("ElevenLabs returned no audio")
    return bytes(buf)


async def usage() -> dict:
    """Credits used this period, for the admin panel."""
    if not configured():
        return {"configured": False}
    try:
        s = await _client().user.subscription.get()
        return {"configured": True, "tier": s.tier, "used": s.character_count,
                "limit": s.character_limit,
                "resets_at": s.next_character_count_reset_unix}
    except Exception as exc:      # noqa: BLE001
        return {"configured": True, "error": _status(exc)}
