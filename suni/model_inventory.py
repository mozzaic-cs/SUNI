"""
Ollama model inventory — scans configured endpoints, tags models with tiers,
and selects the best available model for each tier.

Endpoints are stored in suni_config under "ollama_endpoints" (list of URLs).
Results are cached in memory/model_inventory.json and refreshed at startup
(and on demand via /api/models/inventory?refresh=1).
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .core.model_tier import params_to_tier, TIER_PARAM_RANGES, CLAUDE_CODE_TIER
from . import config as _cfg

_log = logging.getLogger("suni.model_inventory")
_CACHE_PATH = Path("memory/model_inventory.json")
_CACHE_TTL  = 300   # re-use cache if fresher than 5 min

# Preferred model names per tier (tried in order; first match wins)
_TIER_PREFERRED: dict[int, list[str]] = {
    1: ["qwen2.5:3b", "qwen2.5:1.5b", "phi4-mini", "phi3:mini", "gemma2:2b"],
    2: ["qwen2.5:7b", "qwen2.5:14b", "llama3.1:8b", "llama3.2:8b", "mistral:7b"],
    3: ["qwen2.5:32b", "qwen2.5:14b", "llama3.3:70b", "qwen2.5:72b", "mixtral:8x7b"],
    4: ["qwen2.5:72b", "llama3.3:70b", "qwen2.5:32b", "deepseek-r1:70b"],
}


@dataclass
class ModelInfo:
    name:     str
    endpoint: str
    tier:     int
    params_b: float
    size_gb:  float = 0.0
    online:   bool  = True


# ── Parameter extraction ──────────────────────────────────────────────────────

def _parse_params(model_name: str, size_bytes: int = 0) -> float:
    """
    Best-effort extraction of parameter count (billions) from model tag name.
    Falls back to size_bytes heuristic (roughly 0.5 GB per billion params at Q4).
    """
    # Match patterns like :7b, :14b, :0.5b, :1.5b, :72b, _7b, -7b, 7B etc.
    m = re.search(r'[:\-_](\d+(?:\.\d+)?)\s*[bB]\b', model_name)
    if m:
        return float(m.group(1))
    # Embedded without separator: qwen2.5-7b-instruct
    m = re.search(r'(\d+(?:\.\d+)?)\s*[bB](?:\b|-)', model_name, re.IGNORECASE)
    if m:
        return float(m.group(1))
    # Fallback: estimate from file size (Q4: ~0.5 GB/B)
    if size_bytes > 0:
        return round(size_bytes / (1024 ** 3) / 0.55, 1)
    return 7.0   # safe default


# ── Scanner ───────────────────────────────────────────────────────────────────

async def _scan_endpoint(endpoint: str) -> list[ModelInfo]:
    """Return ModelInfo list for all models at one Ollama endpoint."""
    try:
        import ollama
        client = ollama.AsyncClient(host=endpoint)
        data   = await asyncio.wait_for(client.list(), timeout=5.0)
        result = []
        for m in data.models:
            name  = m.model or m.name or ""
            size  = getattr(m, "size", 0) or 0
            pb    = _parse_params(name, size)
            tier  = params_to_tier(pb)
            result.append(ModelInfo(
                name=name, endpoint=endpoint, tier=tier,
                params_b=pb, size_gb=round(size / 1024**3, 2),
            ))
        _log.info("[INVENTORY] %s — found %d model(s)", endpoint, len(result))
        return result
    except Exception as e:
        _log.warning("[INVENTORY] %s — unreachable: %s", endpoint, e)
        return []


async def scan() -> list[ModelInfo]:
    """Scan all configured endpoints and return the full flat model list."""
    endpoints: list[str] = _cfg.get("ollama_endpoints") or [_cfg.ollama_host()]
    tasks     = [_scan_endpoint(ep) for ep in endpoints]
    results   = await asyncio.gather(*tasks)
    models    = [m for batch in results for m in batch]

    # Persist cache
    payload: dict[str, Any] = {
        "ts":     time.time(),
        "models": [asdict(m) for m in models],
    }
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as e:
        _log.warning("[INVENTORY] cache write failed: %s", e)

    return models


def load_cached() -> list[ModelInfo]:
    """Return cached inventory; empty list if cache missing or stale."""
    try:
        raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if time.time() - raw.get("ts", 0) > _CACHE_TTL:
            return []
        return [ModelInfo(**m) for m in raw.get("models", [])]
    except Exception:
        return []


async def get_inventory(force_refresh: bool = False) -> list[ModelInfo]:
    """Return inventory, refreshing if cache is stale or force_refresh=True."""
    if not force_refresh:
        cached = load_cached()
        if cached:
            return cached
    return await scan()


# ── Tier → model selection ────────────────────────────────────────────────────

def best_model_for_tier(
    tier: int,
    inventory: list[ModelInfo],
) -> ModelInfo | None:
    """
    Return the best available ModelInfo for the requested tier.

    Selection priority:
      1. Exact match on tier, preferring names in _TIER_PREFERRED order
      2. Adjacent tier (tier-1 then tier+1) if nothing on the requested tier
    """
    if not inventory:
        return None

    preferred = _TIER_PREFERRED.get(tier, [])

    def _score(m: ModelInfo) -> int:
        try:
            return preferred.index(m.name)
        except ValueError:
            return len(preferred)    # unknown models last

    candidates = [m for m in inventory if m.tier == tier and m.online]
    if candidates:
        return min(candidates, key=_score)

    # Fallback: nearest lower tier first, then higher
    for fallback in [tier - 1, tier + 1, tier - 2, tier + 2]:
        cands = [m for m in inventory if m.tier == fallback and m.online]
        if cands:
            return min(cands, key=_score)

    return None


def tier_map(inventory: list[ModelInfo]) -> dict[int, ModelInfo]:
    """Return {tier: best_ModelInfo} for all tiers that have at least one model.

    Sub-core tiers (the 1–4B "nano", tier 1) are excluded on capable hardware:
    they're too weak for reliable tool use and only cause hallucination/thrash,
    so we never build or route to them. The floor is DEFAULT_TIER (= core on a
    GPU, but still 1 on a CPU-only box where the nano is genuinely all there is,
    so low-end installs keep working)."""
    from .system_profile import DEFAULT_TIER as _FLOOR
    result: dict[int, ModelInfo] = {}
    for tier in sorted(TIER_PARAM_RANGES.keys()):
        if tier < _FLOOR:
            continue                       # never build the nano on capable HW
        m = best_model_for_tier(tier, inventory)
        if m and m.tier >= _FLOOR:         # and don't let fallback pull one in
            result[tier] = m
    return result


def resolve_model(inventory: list[ModelInfo] | None = None) -> str:
    """Effective primary model, in priority order:

        1. the admin-configured model  (config["model"])
        2. the SUNI_MODEL environment variable
        3. the best model actually installed at the core tier
        4. "" — nothing available; the caller reports it as unconfigured

    No model name is hardcoded anywhere. Which model to run is a property of
    the deployment, not of the source: a shipped default is wrong for anyone
    whose hardware or model library differs, and it silently overrides what
    the operator chose in the admin panel.

    That last part was a real defect. The server used to read SUNI_MODEL at
    import only, while saving the admin panel merely patched the live agent —
    so the admin's choice applied until the next restart and was then
    discarded, with nothing to indicate why.

    Lives here rather than in the web server so the CLI resolves identically;
    the two previously disagreed, and the CLI banner reported a different
    fallback again from the model it actually built.

    inventory: models to choose from when nothing is configured. Without it
    this falls back to load_cached(), which is EMPTY on a fresh install —
    the cache is written by a scan that has not happened yet — so step 3
    could never fire in exactly the case it exists for. The server passes its
    startup scan in; found by running the container stack, where SUNI could
    see a model in the Ollama container but still resolved to "".
    """
    import os
    from . import config as _cfg

    configured = str(_cfg.get("model", "") or "").strip()
    if configured:
        return configured

    from_env = os.environ.get("SUNI_MODEL", "").strip()
    if from_env:
        return from_env

    # Nothing chosen: use whatever this machine actually has at the core tier,
    # so a fresh install works without being told about a specific model.
    try:
        from .system_profile import DEFAULT_TIER
        inv = inventory if inventory is not None else load_cached()
        chosen = tier_map(inv).get(DEFAULT_TIER)
        if chosen:
            return chosen.name
    except Exception:
        pass
    return ""
