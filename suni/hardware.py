"""What this machine actually is, and what it is actually good at.

Two different questions, and conflating them is how SUNI has repeatedly chosen
the wrong hardware:

  * The GPU with the most free memory was going to be used for transcription.
    It is a Quadro P600, and putting whisper on it measured 35% SLOWER than the
    CPU.
  * So the rule became "a card with tensor cores". The card with tensor cores
    here measured 0.7x the CPU too.
  * Before that, a 120B model was made the primary because it fitted in RAM.
    It ran at 7.4 tok/s, and `ollama ps` cheerfully reported "100% GPU" while
    most of it sat in host memory.

Every one of those was a sensible inference from a specification, and every one
was wrong. So this module keeps three kinds of fact apart, because they have
three different lifetimes and only one of them is trustworthy for deciding:

STATIC — cores, memory, AVX2, and each card's name, compute capability and
    total VRAM. Cheap, and true until somebody opens the case. Invalidated by
    a fingerprint of itself rather than by a clock.

MEASURED — how fast this machine ACTUALLY does a thing, on each device it could
    do it on. Expensive, never automatic, and the only tier that gets to
    overrule a guess. Stamped with what it measured, because "1.5x realtime"
    without the model, the audio and the device is the kind of note that has
    to be distrusted later.

VOLATILE — free VRAM right now. NEVER written here. It changes minute to minute
    as the language model loads and is evicted, and a consumer that read a
    remembered value would be deciding on a number that was true once.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any

from . import proc as _proc

_PATH = Path("memory/hardware_profile.json")

# Bumped when the SHAPE of a measurement changes, so old numbers are not
# compared against new ones.
MEASURE_VERSION = 1


# ── static ───────────────────────────────────────────────────────────────────

def _avx2() -> bool | None:
    """True, False, or None for "could not tell".

    None is a real answer and is kept as one. Guessing False here would be
    read as "this CPU is slow at inference", which is a decision, and py-cpuinfo
    is not installed to ask properly.
    """
    try:
        if sys.platform == "win32":
            # PF_AVX2_INSTRUCTIONS_AVAILABLE. The documented way to ask
            # Windows, and it needs nothing installed.
            return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))
        if sys.platform.startswith("linux"):
            flags = ""
            for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
                if line.startswith("flags") or line.startswith("Features"):
                    flags = line
                    break
            return (" avx2 " in f" {flags} ") if flags else None
        if sys.platform == "darwin":
            r = _proc.run(["sysctl", "-n", "hw.optional.avx2_0"],
                          capture_output=True, text=True, timeout=5)
            return r.stdout.strip() == "1" if r.returncode == 0 else None
    except Exception:      # noqa: BLE001
        return None
    return None


def cards() -> list[dict]:
    """Each CUDA card: index, name, compute capability, total and free VRAM.

    free_mb is returned but deliberately NOT stored: callers that need it ask
    at the moment they need it.
    """
    try:
        r = _proc.run(["nvidia-smi",
                       "--query-gpu=index,name,compute_cap,memory.total,memory.free",
                       "--format=csv,noheader,nounits"],
                      capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return []
        out = []
        for line in r.stdout.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) < 5:
                continue
            try:
                out.append({"index": int(p[0]), "name": p[1], "cap": float(p[2]),
                            "vram_mb": int(p[3]), "free_mb": int(p[4])})
            except ValueError:
                continue
        return out
    except Exception:      # noqa: BLE001
        return []


_static_cache: dict | None = None


def scan_static(force: bool = False) -> dict:
    """The cheap facts. Safe to run on first start and whenever asked."""
    global _static_cache
    if _static_cache is not None and not force:
        return _static_cache
    cs = cards()
    try:
        import psutil
        cores = psutil.cpu_count(logical=False) or 0
        threads = psutil.cpu_count(logical=True) or 0
        ram_gb = round(psutil.virtual_memory().total / 1024 ** 3, 1)
    except Exception:      # noqa: BLE001
        cores = threads = 0
        ram_gb = 0.0
    out = {
        "os": f"{platform.system()} {platform.release()}",
        "cpu": platform.processor() or platform.machine(),
        "cores": cores,
        "threads": threads,
        "avx2": _avx2(),
        "ram_gb": ram_gb,
        # Per card, and the BIGGEST one rather than the sum. A model runs on
        # one card: two 4 GB cards are not an 8 GB card, and summing them
        # claims a tier that neither could hold.
        "gpus": [{k: v for k, v in c.items() if k != "free_mb"} for c in cs],
        "vram_mb_max": max((c["vram_mb"] for c in cs), default=0),
        "scanned_at": time.time(),
    }
    _static_cache = out
    return out


def fingerprint(static: dict) -> str:
    """Identifies the machine, so measurements are dropped when it changes."""
    key = json.dumps({
        "cpu": static.get("cpu"), "cores": static.get("cores"),
        "ram_gb": static.get("ram_gb"), "avx2": static.get("avx2"),
        "gpus": [(g.get("name"), g.get("vram_mb")) for g in static.get("gpus", [])],
    }, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ── the file ─────────────────────────────────────────────────────────────────

def load() -> dict:
    """The stored profile, or {} when there is none.

    Measurements taken on different hardware are dropped rather than trusted:
    a card swap makes every number here a lie, and a lie with a timestamp is
    worse than nothing.
    """
    try:
        raw = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:      # noqa: BLE001
        return {}
    if not isinstance(raw, dict):
        return {}
    fresh = scan_static()
    if raw.get("fingerprint") != fingerprint(fresh):
        raw["measured"] = {}
        raw["stale_reason"] = "the hardware changed since these were measured"
    raw["static"] = fresh          # static facts are always read live; they are cheap
    return raw


def save(profile: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(profile, indent=2), encoding="utf-8")


def ensure_scanned() -> dict:
    """First run: write the cheap facts. Never measures anything."""
    prof = load()
    if not prof:
        static = scan_static()
        prof = {"fingerprint": fingerprint(static), "static": static, "measured": {}}
        save(prof)
    return prof


# ── measured ─────────────────────────────────────────────────────────────────

def busy_reason() -> str:
    """Why now is a bad moment to benchmark, or "" if it is fine.

    A benchmark holds a card and pegs the cores for minutes. Started during a
    meeting it degrades the very transcription it is trying to make faster.
    """
    try:
        from . import meetings as _m
        if any(_m.active_recording(u) for u in list(getattr(_m, "_active", {}))):
            return "a meeting is being recorded"
    except Exception:      # noqa: BLE001
        pass
    return ""


def record_measurement(kind: str, results: list[dict], context: dict) -> dict:
    """Store one set of numbers, with what produced them.

    `results` is [{device, detail, seconds, realtime_x}], best first. `context`
    says what was measured — the model, the length and shape of the input —
    because a stored "1.5x realtime" with none of that cannot be interpreted
    later, and an old note that cannot be interpreted gets believed.
    """
    prof = ensure_scanned()
    prof.setdefault("measured", {})[kind] = {
        "version": MEASURE_VERSION,
        "at": time.time(),
        "context": context,
        "results": sorted(results, key=lambda r: r.get("seconds", 1e9)),
    }
    prof["fingerprint"] = fingerprint(prof["static"])
    save(prof)
    return prof


def fastest(kind: str) -> dict | None:
    """The device measured fastest for this job, or None if never measured."""
    m = (load().get("measured") or {}).get(kind)
    if not m or m.get("version") != MEASURE_VERSION:
        return None
    results = m.get("results") or []
    return results[0] if results else None



def measured(kind: str) -> dict | None:
    """One measurement set, without rescanning the machine.

    scan_static shells out to nvidia-smi, and this is read at import time by
    system_profile; the memo above means it happens once per process rather
    than once per caller.
    """
    m = (load().get("measured") or {}).get(kind)
    if not m or m.get("version") != MEASURE_VERSION:
        return None
    return m


# ── the benchmark ────────────────────────────────────────────────────────────

_running = False


def _sample_wav(path: str, seconds: int = 30, rate: int = 16000) -> None:
    """A signal to transcribe. Content does not matter and must not.

    Every device is timed on the SAME input, so this is a comparison between
    devices rather than a claim about accuracy. Noise shaped into syllable-like
    bursts keeps the encoder doing real work; VAD is switched off at the call
    site so none of it is skipped, or a device could "win" by doing less.
    """
    import array
    import math
    import random
    import wave

    random.seed(7)                       # same waveform every run, on every machine
    n = seconds * rate
    buf = array.array("h", bytes(2 * n))
    for i in range(n):
        env = 0.5 + 0.5 * math.sin(2 * math.pi * i / (rate * 0.35))   # ~3 per second
        buf[i] = int(max(-32000, min(32000, random.gauss(0, 6000) * env)))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(buf.tobytes())


def measure_whisper(model: str = "base", seconds: int = 30) -> dict:
    """Time whisper on the CPU and on every card, and remember the answer.

    Explicit only. It holds a card and pegs the cores for a minute or two, so
    it refuses while a meeting is being recorded rather than degrading the
    transcription it is meant to speed up.
    """
    global _running
    if _running:
        return {"ok": False, "error": "a measurement is already running"}
    busy = busy_reason()
    if busy:
        return {"ok": False, "error": f"not now — {busy}"}

    import os
    import tempfile
    import time as _time

    _running = True
    tmp = os.path.join(tempfile.gettempdir(), "suni_hw_sample.wav")
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:      # noqa: BLE001
        _running = False
        return {"ok": False, "error": f"faster-whisper is not installed ({exc})"}

    try:
        _sample_wav(tmp, seconds=seconds)
        plans = [("cpu", 0, "int8")]
        for c in cards():
            plans.append(("cuda", c["index"],
                          "float16" if c["cap"] >= 7.0 else "int8_float32"))

        results, errors = [], []
        for dev, idx, comp in plans:
            label = dev if dev == "cpu" else f"cuda:{idx}"
            try:
                kw = {"device": dev, "compute_type": comp, "local_files_only": True}
                if dev == "cuda":
                    kw["device_index"] = idx
                m = WhisperModel(model, **kw)
                t = _time.time()
                segs, _info = m.transcribe(tmp, language="en", vad_filter=False,
                                           beam_size=1)
                for _ in segs:            # a generator: force the work
                    pass
                el = _time.time() - t
                name = ("CPU" if dev == "cpu"
                        else next((c["name"] for c in cards() if c["index"] == idx), label))
                results.append({"device": label, "detail": f"{name} {comp}",
                                "seconds": round(el, 1),
                                "realtime_x": round(seconds / el, 2) if el else 0})
                del m
            except Exception as exc:      # noqa: BLE001
                errors.append(f"{label}: {str(exc)[:120]}")

        if not results:
            return {"ok": False, "error": "; ".join(errors) or "nothing could be measured"}

        record_measurement("whisper", results, {
            "model": model, "audio_seconds": seconds,
            "source": "generated sample, identical on every device",
            "vad_filter": False, "beam_size": 1,
        })
        return {"ok": True, "results": sorted(results, key=lambda r: r["seconds"]),
                "errors": errors}
    finally:
        _running = False
        try:
            os.remove(tmp)
        except OSError:
            pass
