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
from . import hardware as _hw

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


# Weights plus the CUDA context and activations, which are most of it.
_VRAM_NEEDED_MB = {"tiny": 500, "base": 900, "small": 1400,
                   "medium": 3000, "large-v3": 5000}

# Volta and later. Below this a card has no tensor cores, and half precision is
# emulated rather than accelerated.
#
# THIS NUMBER IS THE WHOLE POINT OF THIS FUNCTION, and it is there because of a
# measurement rather than a specification. This machine has a second, idle card
# — a Quadro P600, compute 6.1 — and "use the idle GPU" is the obvious thing to
# do. Measured on 120 seconds of a real meeting: the CPU took 80.3s and the
# P600 took 108.2s. The obvious thing was 35% SLOWER. A picker that chose by
# free memory would have picked it every time.
_MIN_COMPUTE_CAP = 7.0


def _cuda_cards() -> list[dict]:
    """[{index, name, cap, free_mb}] for each card, best capability first.

    Asked through nvidia-smi: there is no Python binding installed and this is
    one call. It runs through suni.proc, so asking does not put a console
    window on someone's screen.
    """
    try:
        from . import proc as _proc
        r = _proc.run(["nvidia-smi",
                       "--query-gpu=index,name,compute_cap,memory.free",
                       "--format=csv,noheader,nounits"],
                      capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return []
        cards = []
        for line in r.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                continue
            try:
                cards.append({"index": int(parts[0]), "name": parts[1],
                              "cap": float(parts[2]), "free_mb": int(parts[3])})
            except ValueError:
                # An older driver that does not know compute_cap. Reporting no
                # cards is the safe answer: it means the CPU, which works.
                return []
        return sorted(cards, key=lambda c: (-c["cap"], -c["free_mb"]))
    except Exception:      # noqa: BLE001 — no driver, no nvidia-smi, no card
        return []


def _second_best_note() -> str:
    """For the log line: what the next-fastest device was. A decision reads as
    a failure unless it says what it chose over."""
    try:
        m = (_hw.load().get("measured") or {}).get("whisper") or {}
        rs = m.get("results") or []
        return f"{rs[1]['device']} {rs[1]['seconds']:.0f}s" if len(rs) > 1 else "nothing else"
    except Exception:      # noqa: BLE001
        return "nothing else"


def _pick_device(name: str) -> tuple[str, int, str]:
    """(device, index, compute_type) for this model on this machine."""
    want = str(_cfg.get("meeting_whisper_device", "auto") or "auto").lower()
    if want == "cpu":
        return "cpu", 0, "int8"

    # A MEASUREMENT beats a specification, whenever one exists. The threshold
    # further down is an inference from what the hardware IS, and that
    # inference has now been wrong twice on this same machine: the card with
    # the most free memory lost to the CPU by 35%, and so did the card with
    # tensor cores. Where somebody has actually measured this job here, that
    # answer wins outright.
    # Naming a card is an explicit instruction, so it is read before anything
    # else decides on the caller's behalf.
    forced_first = int(_cfg.get("meeting_whisper_device_index", -1) or -1)
    if forced_first < 0 and want == "auto":
        best = _hw.fastest("whisper")
        if best:
            dev = str(best.get("device") or "")
            if dev == "cpu":
                log.info("[TRANSCRIBE] measured here: the CPU is fastest for whisper "
                         "(%.0fs against %s); using it",
                         best.get("seconds", 0), _second_best_note())
                return "cpu", 0, "int8"
            if dev.startswith("cuda:"):
                try:
                    idx = int(dev.split(":", 1)[1])
                except ValueError:
                    idx = 0
                compute = "float16" if "float16" in str(best.get("detail", "")) else "int8_float32"
                log.info("[TRANSCRIBE] measured here: %s is fastest for whisper (%.0fs)",
                         dev, best.get("seconds", 0))
                return "cuda", idx, compute

        # NOTHING MEASURED, so take the safe option rather than the clever one.
        # The CPU is the path that works on every machine: it cannot run out of
        # VRAM, it cannot fight the language model for a card, and it has no
        # driver that might not match its runtime. It may be slower somewhere —
        # but slower is a complaint, and a transcription that will not start is
        # a fault. A card gets used when it has been shown to be better, or
        # when somebody asks for it by name.
        log.info("[TRANSCRIBE] nothing measured on this machine yet, so whisper "
                 "runs on the CPU. Measure the devices to let it choose, or set "
                 "meeting_whisper_device=cuda to insist.")
        return "cpu", 0, "int8"

    cards = _cuda_cards()
    if not cards:
        if want == "cuda":
            log.warning("[TRANSCRIBE] cuda was asked for and no card answered; using the CPU")
        return "cpu", 0, "int8"

    need = _VRAM_NEEDED_MB.get(name, 1400)
    forced = int(_cfg.get("meeting_whisper_device_index", -1) or -1)

    if forced >= 0:
        chosen = next((c for c in cards if c["index"] == forced), None)
        if chosen is None:
            log.warning("[TRANSCRIBE] card %d was asked for and is not there; using the CPU", forced)
            return "cpu", 0, "int8"
    else:
        fit = [c for c in cards
               if c["cap"] >= _MIN_COMPUTE_CAP and c["free_mb"] >= need]
        if not fit:
            # Say WHY, with the numbers. "Fell back to CPU" on its own is the
            # sort of line that gets read as a failure when it is a decision.
            log.info("[TRANSCRIBE] using the CPU for %r: %s",
                     name, "; ".join(
                         f"card {c['index']} ({c['name']}) "
                         + ("too old, compute %.1f" % c["cap"]
                            if c["cap"] < _MIN_COMPUTE_CAP
                            else f"{c['free_mb']} MiB free, needs {need}")
                         for c in cards))
            return "cpu", 0, "int8"
        chosen = fit[0]

    # float16 on a card with tensor cores is the fast path they exist for.
    return "cuda", chosen["index"], "float16"


def _load(model: str = ""):
    """Load (and keep) the whisper model, on a card when one is worth using.

    This was CPU-only, and on this machine the CPU is a Sandy Bridge Xeon with
    NO AVX2 — the instruction set every fast inference path is written for. An
    hour of meeting pinned every core and ran at about 1.5x realtime, so a
    half-hour recording took twenty minutes and the machine was unusable while
    it did.
    """
    global _model, _model_name
    name = model or str(_cfg.get("meeting_whisper_model", "base") or "base")
    if _model is not None and _model_name == name:
        return _model
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:    # noqa: BLE001
        raise TranscriptionError(f"{_INSTALL_HINT}\n({exc})")
    device, index, compute = _pick_device(name)
    log.info("[TRANSCRIBE] loading whisper %r on %s%s (%s)", name, device,
             f" card {index}" if device == "cuda" else "", compute)
    # Try the local cache FIRST. Without this, every load contacts the Hugging
    # Face hub to check the model is current — measured at ~174s on this machine
    # against ~2s from disk. SUNI has been bitten by exactly this before: local
    # image generation went from 208s to 29s per load for the same reason.
    #
    # The fallback is the download path, so a first run still works; it is only
    # the repeated cost that is removed.
    def _build(dev, idx, comp, cached):
        kw = {"device": dev, "compute_type": comp}
        if dev == "cuda":
            kw["device_index"] = idx
        if cached:
            kw["local_files_only"] = True
        return WhisperModel(name, **kw)

    # The chosen device, then the CPU. A card can be present, capable and still
    # refuse to load — a driver and a cuDNN that disagree is the usual reason,
    # and it throws at load rather than at import. Falling back is the whole
    # difference between slower transcription and none.
    plan = [(device, index, compute)]
    if device != "cpu":
        plan.append(("cpu", 0, "int8"))

    _model = None
    for dev, idx, comp in plan:
        try:
            _model = _build(dev, idx, comp, True)
            log.info("[TRANSCRIBE] loaded %r from the local cache on %s", name, dev)
            break
        except Exception:                   # noqa: BLE001 — or not cached yet
            try:
                _model = _build(dev, idx, comp, False)
                log.info("[TRANSCRIBE] downloaded %r and loaded it on %s", name, dev)
                break
            except Exception as exc:        # noqa: BLE001
                log.warning("[TRANSCRIBE] %s would not load %r (%s); %s", dev, name,
                            str(exc)[:120],
                            "trying the CPU" if dev != "cpu" else "giving up")
    if _model is None:
        raise TranscriptionError(f"whisper model {name!r} could not be loaded")
    _model_name = name
    return _model


def _transcribe_sync(path: str, language: str | None,
                     model_name: str = "") -> list[dict]:
    model = _load(model_name)
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


async def transcribe_file(path: str | Path, language: str = "",
                          model: str = "") -> list[dict]:
    """Transcribe a recording into timestamped segments.

    Runs in a worker thread: the model call is long and fully blocking, and the
    event loop is serving the rest of SUNI while a meeting is processed.
    """
    p = Path(path)
    if not p.exists():
        raise TranscriptionError(f"No such recording: {p}")
    lang = (language or str(_cfg.get("stt_language", "")) or "").split("-")[0]
    try:
        return await asyncio.to_thread(_transcribe_sync, str(p), lang or None, model)
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
