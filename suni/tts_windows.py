"""The voices Windows itself ships, rendered on the server.

Why server-side and not the browser's speechSynthesis: the browser speaks
straight to the speakers, so the page's analyser never sees the audio and her
mouth would stop following her voice. Rendered here, a Windows voice is just
another WAV through the same path as every other voice.

Windows 10 offers European Portuguese as an optional download (Microsoft
Helia: Settings > Time & Language > Speech > Add voices). Those "OneCore"
voices are not in the classic SAPI registry list, so the category is pointed
at Speech_OneCore explicitly; the classic list is read too.

Measured: a full sentence in ~0.3 s. Not neural — clearly synthetic — but a
European Portuguese female voice that never leaves the machine, which is what
was missing when Microsoft's cloud began refusing edge-tts (403).

Every call runs in a thread: COM synthesis blocks, and this event loop has
been starved before by smaller things.
"""
from __future__ import annotations

import asyncio
import io
import locale
import logging
import sys
import threading
import wave

_log = logging.getLogger("suni.tts_windows")

PREFIX = "win:"
_CATEGORIES = (
    r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices",
    r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices",
)
_RATE = 22050
_lock = threading.Lock()          # one COM synthesis at a time
_cache: list[tuple[str, str]] | None = None   # [(description, "pt-PT"), ...]


def _tokens():
    """(description, language tag, token) for every installed voice, deduped."""
    import win32com.client
    seen, out = set(), []
    for cat_id in _CATEGORIES:
        try:
            cat = win32com.client.Dispatch("SAPI.SpObjectTokenCategory")
            cat.SetId(cat_id, False)
            toks = cat.EnumerateTokens()
        except Exception:      # noqa: BLE001 — a missing category is not an error
            continue
        for i in range(toks.Count):
            t = toks.Item(i)
            desc = t.GetDescription()
            if desc in seen:
                continue
            seen.add(desc)
            lang = ""
            try:
                lcid = int(t.GetAttribute("Language").split(";")[0], 16)
                lang = locale.windows_locale.get(lcid, "").replace("_", "-")
            except Exception:      # noqa: BLE001
                pass
            out.append((desc, lang, t))
    return out


def _with_com(fn):
    import pythoncom
    pythoncom.CoInitialize()
    try:
        return fn()
    finally:
        pythoncom.CoUninitialize()


def voices() -> list[tuple[str, str]]:
    """[(description, language)] of the installed Windows voices. Cached:
    voices are installed rarely, and this is asked on every request."""
    global _cache
    if _cache is None:
        if sys.platform != "win32":
            _cache = []
        else:
            try:
                with _lock:
                    _cache = _with_com(lambda: [(d, l) for d, l, _ in _tokens()])
            except Exception as exc:      # noqa: BLE001
                _log.info("[TTS] no Windows voices: %s", exc)
                _cache = []
    return _cache


def voice_ids() -> list[str]:
    return [PREFIX + d for d, _ in voices()]


def for_language(lang: str) -> str | None:
    """The first installed voice for a language tag ("pt-PT"), as an id."""
    lang = (lang or "").lower()
    for d, l in voices():
        if l.lower() == lang:
            return PREFIX + d
    return None


def _synth_sync(desc: str, text: str) -> bytes:
    import win32com.client

    def run():
        tok = next((t for d, _, t in _tokens() if d == desc), None)
        if tok is None:
            return b""
        v = win32com.client.Dispatch("SAPI.SpVoice")
        v.Voice = tok
        fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
        fmt.Type = 22                                   # SAFT22kHz16BitMono
        ms = win32com.client.Dispatch("SAPI.SpMemoryStream")
        ms.Format = fmt
        v.AudioOutputStream = ms
        v.Speak(text, 16)           # SVSFIsNotXML, synchronous: text is never markup
        return bytes(ms.GetData())

    with _lock:
        pcm = _with_com(run)
    if not pcm:
        return b""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


async def speak(text: str, voice_id: str) -> bytes | None:
    """WAV for `text` in the Windows voice `voice_id` ("win:<description>"),
    or None — the caller then falls back as it would for any failed voice."""
    text = (text or "").strip()
    if not text or not voice_id.startswith(PREFIX) or sys.platform != "win32":
        return None
    try:
        data = await asyncio.to_thread(_synth_sync, voice_id[len(PREFIX):], text)
    except Exception as exc:      # noqa: BLE001 — a failed clip is not a failed reply
        _log.warning("[TTS] Windows voice failed: %s", exc)
        return None
    return data or None
