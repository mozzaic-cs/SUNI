"""
System resource profiler — detects RAM, VRAM, and CPU at startup and
derives appropriate operating parameters for all SUNI subsystems.

Computed once at import time; values are constants for the process lifetime.
All derived limits are logged at startup so they appear in the daily log.
"""
from __future__ import annotations
import logging
import os
from . import proc as _proc

_log = logging.getLogger("suni.system_profile")


def _ram_gb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().total / 1024 ** 3
    except Exception:
        return 8.0   # conservative fallback


def _vram_mb() -> int:
    """VRAM (MB) of the BIGGEST single card, not the sum of all of them.

    It used to sum. A model runs on ONE card: two 4 GB cards are not an 8 GB
    card, and summing them claims a tier that neither of them could hold. On
    this machine the sum is 10,238 MB across an 8 GB card and a 2 GB one, and
    it happens not to cross a tier boundary — which is exactly how a wrong
    number survives, by being wrong somewhere it does not yet show.
    """
    try:
        import torch
        if torch.cuda.is_available():
            return max(
                (torch.cuda.get_device_properties(i).total_memory // 1024 ** 2
                 for i in range(torch.cuda.device_count())),
                default=0,
            )
    except Exception:
        pass
    try:
        import subprocess
        r = _proc.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip().isdigit()]
        if lines:
            return max(int(l) for l in lines)
    except Exception:
        pass
    return 0


def _cpu_cores() -> int:
    try:
        import psutil
        return psutil.cpu_count(logical=False) or 4
    except Exception:
        return 4


# ── Detected hardware ─────────────────────────────────────────────────────────

RAM_GB:   float = _ram_gb()
VRAM_MB:  int   = _vram_mb()
CPU_CORES: int  = _cpu_cores()


# ── Derived parameters ────────────────────────────────────────────────────────

# Document ingestion — max single-file size before skipping
# 2% of RAM, capped at 4 GB. On 192 GB → 3.84 GB (effectively no limit for docs).
MAX_DOC_FILE_MB: int = min(int(RAM_GB * 0.02 * 1024), 4096)

# Embedding batch size — more RAM → larger batches → higher throughput on CPU
# On 192 GB → 256; on 8 GB → 32.
EMBED_BATCH_SIZE: int = min(max(32, int(RAM_GB * 1.33)), 512)

# Concurrent file ingestion — how many files to embed in parallel asyncio tasks
# Controlled by a semaphore in the scanner. 1 per 32 GB RAM, min 1, max 16.
INGEST_CONCURRENCY: int = min(max(1, int(RAM_GB // 32)), 16)

# Ollama context window — num_ctx tokens.
# Keep the KV cache RESIDENT IN VRAM. Letting it overflow to RAM (as a larger
# num_ctx does on an 8 GB card) makes attention read from system memory, which
# crawls prompt eval (~500 tok/s vs thousands). On an 8 GB card, a 7B (~4.7 GB) +
# an 8192-token KV cache (~0.95 GB) + buffers fits comfortably and stays fast.
# Each KV token ≈ 0.116 MB for qwen2.5:7b (28 layers, 8 KV heads, 128 dim, fp16).
if VRAM_MB >= 12000:
    NUM_CTX: int = 16384   # big card — KV cache still fits in VRAM
elif VRAM_MB >= 6000:
    NUM_CTX = 8192         # 8 GB class — resident, fast (no RAM spill)
else:
    NUM_CTX = 4096         # safe minimum

# Context history (max messages in Context class)
# More RAM → keep more history before max_history trim kicks in
# 60 baseline; add 10 per 32 GB of extra RAM beyond 8 GB
NUM_HISTORY: int = min(60 + max(0, int((RAM_GB - 8) // 32)) * 10, 200)

# Context compressor threshold — estimated tokens before compression fires
# With more RAM we can afford a larger context before compressing
# 4800 baseline; scale up with num_ctx headroom (keep ~40% free for generation)
COMPRESS_THRESHOLD: int = int(NUM_CTX * 0.58)


def effective_num_ctx() -> int:
    """The context size every Ollama caller uses: config `num_ctx`, else NUM_CTX.

    NUM_CTX is sized for a model whose weights live on the card. A large MoE
    that mostly runs from system RAM (gpt-oss:120b) needs more room than that
    derivation allows, so the configured value wins. Resolved per call because
    the admin panel edits it at runtime.
    """
    try:
        from . import config as _c
        return int(_c.get("num_ctx", NUM_CTX) or NUM_CTX)
    except Exception:      # noqa: BLE001
        return NUM_CTX


def compress_threshold() -> int:
    """COMPRESS_THRESHOLD, scaled to the effective context rather than NUM_CTX."""
    return int(effective_num_ctx() * 0.58)

# Safety rescan interval for watchdog (seconds)
# Same regardless of RAM — 24h is sufficient
SAFETY_RESCAN_S: int = 86400


# ── Model tier feasibility ────────────────────────────────────────────────────
#
# Tiers based on model parameter count:
#   Tier 1 (nano):  1–4 B   — fast Q&A, lookups, formatting
#   Tier 2 (core):  5–14 B  — default balanced model          ← current qwen2.5:7b
#   Tier 3 (large): 15–44 B — complex reasoning, analysis
#   Tier 4 (max):   45 B+   — maximum local capability
#   Tier 5:         Claude Code (claude_task) — no GPU needed, always available
#
# VRAM thresholds assume Q4_K_M quantisation (≈0.5 GB/B of params + 1 GB overhead).

# Somebody else is already using the card. A desktop compositor holds 2.6 GB of
# the 8 GB card on this machine — measured — and a model needs its KV cache on
# top of its weights. Claiming all of it is how a 7B ends up paging to host
# memory while every specification says it fits.
_VRAM_RESERVE_MB = 2_048

# Below this, a local model is not worth reaching for: the answer arrives, but
# not while anybody is still waiting for it. From measurement rather than
# taste — gpt-oss:120b ran here at 7.4 tok/s and was unusable, while a 1.5B on
# the same card managed 153.
INTERACTIVE_FLOOR_TPS = 15.0


def _measured_max_tier() -> int | None:
    """The highest tier MEASURED fast enough to be worth using, or None.

    None means nobody has measured; 1 means somebody did and nothing cleared
    the floor. Those are different answers and must not collapse into each
    other — the second one is knowledge.
    """
    try:
        from . import hardware as _hw
        m = _hw.measured("llm")
        if not m:
            return None
        usable = [int(r.get("tier", 0)) for r in (m.get("results") or [])
                  if float(r.get("tokens_per_sec", 0)) >= INTERACTIVE_FLOOR_TPS]
        return max(usable) if usable else 1
    except Exception:      # noqa: BLE001
        return None


def _max_local_tier(vram_mb: int) -> int:
    """How big a model this machine should reach for locally.

    MEASURED first. What this used to be was an inference from a specification,
    and it over-claimed by a whole tier here: 8 GB of VRAM was read as "can run
    a 15-44B model", on a box where a 7B already pages because the desktop has
    2.6 GB of the card. The same reasoning is what made a 120B the primary at
    7.4 tok/s.

    Unmeasured, it now takes the safe reading: reserve what something else is
    using, and require the SMALLEST model in a tier to fit with its cache
    rather than the tier's name to sound affordable. A tier that is claimed and
    cannot be delivered is worse than one that is never offered — the model
    loads, runs at walking pace, and every answer is late.
    """
    measured = _measured_max_tier()
    if measured is not None:
        return max(1, min(4, measured))

    usable = max(0, vram_mb - _VRAM_RESERVE_MB)
    # Q4 is roughly 0.6 GB per billion parameters, and the cache is on top.
    # These are the tier MINIMUMS: to claim a tier you must fit its smallest
    # member, not its most flattering one.
    if usable >= 32_000: return 4    # 45B+ @ Q4 ≈ 27 GB + cache
    if usable >= 12_000: return 3    # 15B  @ Q4 ≈  9 GB + cache
    if usable >=  5_000: return 2    #  5B  @ Q4 ≈  3 GB + cache
    return 1                          #  1-4B, or the CPU


MAX_LOCAL_TIER: int  = _max_local_tier(VRAM_MB)
FEASIBLE_TIERS: list = list(range(1, MAX_LOCAL_TIER + 1))
DEFAULT_TIER:   int  = min(2, MAX_LOCAL_TIER)   # prefer core; fall to nano on low-VRAM


def log_profile() -> None:
    _log.info(
        "[PROFILE] RAM=%.0f GB  VRAM=%d MB  CPU=%d cores | "
        "max_doc=%.0f MB  batch=%d  concurrency=%d  num_ctx=%d  history=%d  compress_at=%d tok | "
        "tier_max=%d  tier_default=%d  feasible=%s",
        RAM_GB, VRAM_MB, CPU_CORES,
        MAX_DOC_FILE_MB, EMBED_BATCH_SIZE, INGEST_CONCURRENCY,
        NUM_CTX, NUM_HISTORY, COMPRESS_THRESHOLD,
        MAX_LOCAL_TIER, DEFAULT_TIER, FEASIBLE_TIERS,
    )
