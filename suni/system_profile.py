"""
System resource profiler — detects RAM, VRAM, and CPU at startup and
derives appropriate operating parameters for all SUNI subsystems.

Computed once at import time; values are constants for the process lifetime.
All derived limits are logged at startup so they appear in the daily log.
"""
from __future__ import annotations
import logging
import os

_log = logging.getLogger("suni.system_profile")


def _ram_gb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().total / 1024 ** 3
    except Exception:
        return 8.0   # conservative fallback


def _vram_mb() -> int:
    """Return TOTAL VRAM (MB) summed across all CUDA devices visible to this process."""
    try:
        import torch
        if torch.cuda.is_available():
            return sum(
                torch.cuda.get_device_properties(i).total_memory // 1024 ** 2
                for i in range(torch.cuda.device_count())
            )
    except Exception:
        pass
    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip().isdigit()]
        if lines:
            return sum(int(l) for l in lines)
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

def _max_local_tier(vram_mb: int) -> int:
    if vram_mb >= 36_000: return 4   # 70B @ Q4 ≈ 35 GB
    if vram_mb >= 18_000: return 3   # 34B @ Q4 ≈ 17 GB
    if vram_mb >= 7_000:  return 3   # 14B @ Q4 ≈  7 GB
    if vram_mb >= 4_000:  return 2   #  7B @ Q4 ≈  4 GB
    if vram_mb >= 1_500:  return 1   #  3B @ Q4 ≈  2 GB
    return 1                          # CPU fallback — nano only


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
