"""Speech without a round trip.

Measured on this machine, per sentence, European Portuguese:

    edge-tts (cloud)   ~2.0-2.5 s to first audio, every time
    piper (local)       0.21 s, about eleven times realtime

The cloud call is the single biggest cost in the voice path. Replies are
already spoken sentence by sentence, so that two seconds is not paid once — it
is paid before the first sentence and then raced against playback for every one
after it, and any hiccup opens a gap. It is also a dependency on Microsoft
being up and reachable: the memory-store bloat incident showed edge-tts
returning 503 when this box was merely busy.

Piper is ONNX on the CPU. This is a Sandy Bridge Xeon with NO AVX2, which is
where ONNX usually disappoints, and it still ran eleven times faster than
realtime — so the measurement above is a floor, not a best case.

TWO THINGS THIS FILE IS CAREFUL ABOUT:

  Synthesis is CPU-bound and MUST NOT run on the event loop. This codebase has
  starved that loop three times — a per-file index write, an os.walk, a 2 MB
  state save — and each one looked like "SUNI is hanging" rather than like a
  blocked thread. Every call here goes through a thread.

  It degrades to edge-tts rather than failing. A missing voice file, an ONNX
  that will not load on some other machine, an unsupported language: none of
  those should cost somebody their voice output. The caller asks; if the answer
  is None it uses what it used before.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import wave
from pathlib import Path

_log = logging.getLogger("suni.tts_local")

# Where voices live. A voice is two files: <name>.onnx and <name>.onnx.json.
#
# Anchored to the INSTALL, not to the working directory. "models/piper" is a
# relative path, and a relative path is a promise about where the process was
# started — which a systemd unit, a Windows service or a test harness all break
# without telling anyone. The test suite chdirs to a temp directory on purpose,
# and the voice vanished; started as a service it would have vanished the same
# way, silently falling back to the cloud with nothing to say why.
_ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = _ROOT / "models" / "piper"

_voice = None          # the loaded PiperVoice, or False once we know it cannot load
_lock = asyncio.Lock()


def model_path() -> Path | None:
    """The configured voice, or the only one present, or nothing."""
    try:
        from . import config as _cfg
        named = str(_cfg.get("piper_voice", "") or "").strip()
    except Exception:      # noqa: BLE001
        named = ""
    if named:
        p = MODEL_DIR / (named if named.endswith(".onnx") else f"{named}.onnx")
        return p if p.exists() else None
    found = sorted(MODEL_DIR.glob("*.onnx")) if MODEL_DIR.exists() else []
    return found[0] if found else None


def available() -> bool:
    """Could this speak, if asked? Cheap enough to call per request."""
    if _voice is False:
        return False
    return model_path() is not None


def _load_sync():
    from piper import PiperVoice
    path = model_path()
    if path is None:
        return None
    return PiperVoice.load(str(path))


async def _get_voice():
    """The loaded voice, loading it once.

    Held for the life of the process on purpose: loading costs ~5 s and the
    first synthesis after a load costs another ~2 s of ONNX warm-up, which is
    the whole saving thrown away if it happens per request.
    """
    global _voice
    if _voice is not None:
        return _voice or None
    async with _lock:
        if _voice is not None:
            return _voice or None
        try:
            v = await asyncio.to_thread(_load_sync)
        except Exception as exc:      # noqa: BLE001
            _log.warning("[TTS] piper unavailable, staying on the cloud voice: %s", exc)
            _voice = False
            return None
        if v is None:
            _voice = False
            return None
        _voice = v
        _log.info("[TTS] piper voice loaded: %s", model_path().name)
        return v


def _synth_sync(voice, text: str) -> bytes:
    """WAV bytes. Piper yields PCM chunks; the header needs the first one's
    rate and width, so the file is written as they arrive rather than guessed."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        first = True
        for ch in voice.synthesize(text):
            if first:
                w.setnchannels(ch.sample_channels)
                w.setsampwidth(ch.sample_width)
                w.setframerate(ch.sample_rate)
                first = False
            w.writeframes(ch.audio_int16_bytes)
        if first:                      # nothing came back
            return b""
    return buf.getvalue()


async def speak(text: str) -> bytes | None:
    """WAV audio for `text`, or None if the caller should use the cloud voice."""
    text = (text or "").strip()
    if not text:
        return None
    voice = await _get_voice()
    if voice is None:
        return None
    try:
        data = await asyncio.to_thread(_synth_sync, voice, text)
    except Exception as exc:      # noqa: BLE001 — a failed clip is not a failed reply
        _log.warning("[TTS] piper synthesis failed, falling back: %s", exc)
        return None
    return data or None


async def warm() -> None:
    """Load the voice and push one phrase through it at startup.

    The first synthesis after a load costs about two seconds of ONNX warm-up.
    Paying it here means the first thing anybody hears is not the slowest thing
    they will ever hear.
    """
    if not available():
        return
    try:
        await speak("Pronta.")
        _log.info("[TTS] piper warmed")
    except Exception:      # noqa: BLE001
        pass
