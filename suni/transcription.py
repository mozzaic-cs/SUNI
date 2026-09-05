"""
Turning a recording into text, locally.

Deliberately CPU-only. The GPU already holds a 7B model in 8 GB of shared VRAM,
and a transcription pass that evicts it would make every chat slow for as long
as the meeting takes to process. A meeting is finished by the time this runs, so
nothing is waiting on it — trading speed for not disturbing the assistant is the
right way round.

Measured on this hardware (16-core desktop, `base`, int8, VAD on): **4.4x faster
than real time**, so a 60-minute meeting transcribes in roughly 14 minutes. Model
load is ~2-5s from the local cache, and the first ever run additionally
downloads ~150 MB.

`faster-whisper` is an OPTIONAL dependency (requirements-meetings.txt), in the
same way image generation is. If it is absent the caller is told exactly that,
with the install line, rather than getting an ImportError traceback.

Output is SEGMENTS, not one wall of text. Timestamps make a summary checkable —
"they agreed at 14:32" can be found and listened to — and they are what lets a
long meeting be summarised in pieces without cutting a sentence in half.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from . import config as _cfg
from .logger import get_logger

log = get_logger("suni.transcription")

_INSTALL_HINT = (
    "Local transcription needs faster-whisper. Install it with:\n"
    "    pip install -r requirements-meetings.txt")

# Cached across calls: loading the model costs seconds and several hundred MB,
# and a meeting is transcribed in many chunks.
_model = None
_model_name = ""


class TranscriptionError(RuntimeError):
    """Reported to the user as-is."""


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:           # noqa: BLE001
        return False


def _load():
    """Load (and keep) the whisper model. CPU, int8 — the quantisation is what
    makes a CPU pass tolerable rather than an overnight job."""
    global _model, _model_name
    name = str(_cfg.get("meeting_whisper_model", "base") or "base")
    if _model is not None and _model_name == name:
        return _model
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:    # noqa: BLE001
        raise TranscriptionError(f"{_INSTALL_HINT}\n({exc})")
    log.info("[TRANSCRIBE] loading whisper model %r on CPU (int8)", name)
    # Try the local cache FIRST. Without this, every load contacts the Hugging
    # Face hub to check the model is current — measured at ~174s on this machine
    # against ~2s from disk. SUNI has been bitten by exactly this before: local
    # image generation went from 208s to 29s per load for the same reason.
    #
    # The fallback is the download path, so a first run still works; it is only
    # the repeated cost that is removed.
    try:
        _model = WhisperModel(name, device="cpu", compute_type="int8",
                              local_files_only=True)
        log.info("[TRANSCRIBE] loaded %r from the local cache", name)
    except Exception:                       # noqa: BLE001 — not cached yet
        log.info("[TRANSCRIBE] %r not cached; downloading it once", name)
        _model = WhisperModel(name, device="cpu", compute_type="int8")
    _model_name = name
    return _model


def _transcribe_sync(path: str, language: str | None) -> list[dict]:
    model = _load()
    # vad_filter drops silence, which on a meeting recording is most of it —
    # nobody talks over anybody for the full hour, and skipping the gaps is the
    # single biggest speed win available on CPU.
    segments, _info = model.transcribe(
        path,
        language=language or None,
        vad_filter=True,
        beam_size=1,            # greedy: on CPU the accuracy gain is not worth 3x
    )
    return [
        {"start": round(s.start, 1), "end": round(s.end, 1), "text": s.text.strip()}
        for s in segments if s.text and s.text.strip()
    ]


async def transcribe_file(path: str | Path, language: str = "") -> list[dict]:
    """Transcribe a recording into timestamped segments.

    Runs in a worker thread: the model call is long and fully blocking, and the
    event loop is serving the rest of SUNI while a meeting is processed.
    """
    p = Path(path)
    if not p.exists():
        raise TranscriptionError(f"No such recording: {p}")
    lang = (language or str(_cfg.get("stt_language", "")) or "").split("-")[0]
    try:
        return await asyncio.to_thread(_transcribe_sync, str(p), lang or None)
    except TranscriptionError:
        raise
    except Exception as exc:    # noqa: BLE001
        raise TranscriptionError(f"Transcription failed: {exc}")


def to_text(segments: list[dict], timestamps: bool = True) -> str:
    """Segments as readable text."""
    if not timestamps:
        return " ".join(s["text"] for s in segments)
    out = []
    for s in segments:
        m, sec = divmod(int(s["start"]), 60)
        out.append(f"[{m:02d}:{sec:02d}] {s['text']}")
    return "\n".join(out)


def chunk(segments: list[dict], max_chars: int = 6000) -> list[str]:
    """Split a transcript into pieces a small model can actually read.

    An hour of talking is roughly ten thousand words, and the local tier runs
    with num_ctx 8192 — so a single-pass summary silently loses most of the
    meeting. Splitting on SEGMENT boundaries rather than character count means
    no chunk starts mid-sentence, which is what makes a per-chunk summary read
    like prose instead of fragments.
    """
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for s in segments:
        line = s["text"]
        if size + len(line) > max_chars and cur:
            chunks.append(" ".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append(" ".join(cur))
    return chunks
