"""
Memory consolidation: LLM-powered extraction of durable facts from conversation
history, semantic supersession of contradicting facts, and cosine-similarity
deduplication — all via reversible soft-deprecation rather than hard deletion.

Pipeline (consolidate())
-------------------------
1. extract_facts()      — LLM parses recent conversation entries into new
                          [FACT]/[PREFERENCE] entries (source=llm-extracted)
2. supersede_facts()    — LLM detects contradicting facts about the same
                          attribute ("works at X" → "works at Y") and deprecates
                          the stale one (reason=superseded). Embedding similarity
                          is NOT used here: contradictions score LOW cosine (the
                          differing entity dominates the vector) while independent
                          facts score high, so only a semantic model is reliable.
3. dedup()              — near-duplicate fact/preference entries (cosine ≥
                          threshold) collapse to the newest (reason=duplicate)
4. dedup_conversations() — same for verbatim-ish conversation entries
5. age_out()            — conversation entries older than N days deprecated
                          (reason=aged_out); 0 = disabled

All removals are SOFT: entries get metadata.lifecycle="deprecated" and stay on
disk (excluded from recall, available for audit/rollback). Nothing is destroyed.

Trigger: weekly background scheduler (start_consolidation_scheduler) or manual
POST /api/memory/consolidate from the admin panel.

Locking: asyncio.Lock keyed by store path prevents two passes from running
concurrently for the same user.
"""
from __future__ import annotations
import asyncio
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from .. import config as _config

log = logging.getLogger("suni.consolidator")

DEDUP_THRESHOLD    = 0.92   # fallback: cosine above which two facts are duplicates
CONV_DEDUP_THRESH  = 0.95   # fallback: cosine above which two conversations dupe
EXTRACTION_BATCH   = 20     # max conversation entries to send per extraction pass
SUPERSEDE_CAP      = 60     # max active facts sent to the supersession LLM pass
def LLM_HOST() -> str:                          # noqa: N802 (kept as the old name)
    """Resolved per call — see config.ollama_host()."""
    from .. import config as _c
    return _c.ollama_host()
LLM_MODEL_FALLBACK = "qwen2.5:7b"

_EXTRACT_SYSTEM = (
    "You analyze conversation history to extract durable facts about a user.\n"
    "Output only fact lines in exactly this format — nothing else:\n"
    "[FACT] <one sentence fact about the user>\n"
    "[PREFERENCE] <one sentence user preference>\n\n"
    "Rules:\n"
    "- Only include information clearly stated by the user, not inferred\n"
    "- Focus on: identity, occupation, location, relationships, ongoing work, explicit preferences\n"
    "- Skip greetings, assistant replies, and transient context\n"
    "- Return nothing if no durable facts are present"
)

_EXTRACT_USER = "Extract durable facts from these conversation exchanges:\n\n{exchanges}"

# Appended to the extraction prompt ONLY when org extraction is enabled, so that
# "off" is byte-identical to the behaviour before this existed — the model never
# sees the marker, never emits it, and every fact stays personal.
#
# This rides the extraction call that already happens rather than adding a
# second pass. A separate org-relevance call would double the LLM cost of every
# consolidation on a 7B that is already short of context.
_ORG_RULES = (
    "\n- Use [ORG] instead of [FACT] when the information is about the "
    "ORGANISATION rather than the person: suppliers, clients, products, "
    "processes, policies, deadlines, decisions a colleague would need to know.\n"
    "- Information about the user themselves is never [ORG], even work-related: "
    "their role, their preferences and their own projects stay [FACT].\n"
    "- [ORG] lines are reviewed by a human before anyone else can read them, so "
    "prefer marking too few rather than too many."
)

_SUPERSEDE_SYSTEM = (
    "You are given a numbered list of stored facts about one user, ordered "
    "oldest first. Find pairs where a LATER fact updates or contradicts an "
    "EARLIER fact about the SAME attribute (e.g. same employer, same city, same "
    "marital status, same current project), making the earlier one no longer "
    "true.\n\n"
    "Output only lines in exactly this format — nothing else:\n"
    "OLD <earlier-number> NEW <later-number>\n\n"
    "Rules:\n"
    "- Only pair facts about the SAME attribute where the later one truly replaces the earlier\n"
    "- Two different-but-similar facts (e.g. likes coffee / likes tea) are NOT a pair\n"
    "- The NEW number must be greater than the OLD number\n"
    "- If you are unsure, output nothing. It is far better to miss a pair than to invent one."
)

_SUPERSEDE_USER = "Stored facts (oldest first):\n\n{facts}"

# Per-store-path locks — prevent overlapping consolidation passes for the same user
_locks: dict[str, asyncio.Lock] = {}


def _get_lock(store_path: str) -> asyncio.Lock:
    if store_path not in _locks:
        _locks[store_path] = asyncio.Lock()
    return _locks[store_path]


def _cfg(key: str, fallback):
    try:
        return _config.get(key, fallback)
    except Exception:
        return fallback


def _active(entry: dict) -> bool:
    return (entry.get("metadata") or {}).get("lifecycle") != "deprecated"


async def _call_ollama(prompt: str, system: str, host: str, model: str) -> str:
    try:
        import ollama
        client = ollama.AsyncClient(host=host or LLM_HOST())
        resp = await client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": prompt},
            ],
            options={"temperature": 0.1, "num_predict": 512},
        )
        return resp["message"]["content"].strip()
    except Exception as exc:
        log.warning("[CONSOLIDATOR] LLM call failed: %s", exc)
        return ""


def _parse_extractions(text: str) -> list[tuple[str, str]]:
    """Parse LLM output → list of (content, memory_type)."""
    results: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^\[FACT\]\s+(.+)$", line, re.IGNORECASE)
        if m:
            results.append((m.group(1).strip(), "fact"))
            continue
        m = re.match(r"^\[PREFERENCE\]\s+(.+)$", line, re.IGNORECASE)
        if m:
            results.append((m.group(1).strip(), "preference"))
            continue
        # Only emitted when org extraction is on — the marker is not in the
        # prompt otherwise. Parsing it unconditionally is harmless and means a
        # stray marker cannot silently become a personal fact.
        m = re.match(r"^\[ORG\]\s+(.+)$", line, re.IGNORECASE)
        if m:
            results.append((m.group(1).strip(), "org"))
    return results


async def extract_facts(
    manager,
    host: str = "",
    model: str = LLM_MODEL_FALLBACK,
    since_ts: str | None = None,
) -> int:
    """
    Run LLM extraction on recent conversation entries.
    Returns number of new fact/preference entries added.
    """
    store = manager.store
    entries = store.get_all()

    conversations = [
        e for e in entries
        if e["type"] == "conversation"
        and _active(e)
        and (since_ts is None or e["timestamp"] > since_ts)
        and e.get("metadata", {}).get("source") != "llm-extracted"
    ]
    # Most recent first, capped at batch size
    conversations = sorted(conversations, key=lambda e: e["timestamp"], reverse=True)
    conversations = conversations[:EXTRACTION_BATCH]

    if not conversations:
        return 0

    # Off by default: this turns one person's conversation into memory other
    # people can read, which is a decision an operator makes rather than
    # something that starts happening after an upgrade.
    org_on = bool(_cfg("memory_org_extraction", False)) and \
        getattr(manager, "collective_store", None) is not None
    system_prompt = _EXTRACT_SYSTEM + (_ORG_RULES if org_on else "")

    exchanges_text = "\n---\n".join(e["content"] for e in conversations)
    llm_output = await _call_ollama(
        _EXTRACT_USER.format(exchanges=exchanges_text), system_prompt, host, model
    )

    extractions = _parse_extractions(llm_output)
    if not extractions:
        return 0

    now_ts = datetime.now().isoformat()
    personal = staged = 0
    for content, mtype in extractions:
        if mtype == "org":
            if not org_on:
                # The marker cannot normally appear with the feature off, but a
                # model that emits it anyway must not have the line silently
                # dropped — keep it as a personal fact, which is the behaviour
                # that existed before org extraction did.
                await manager.add(content, memory_type="fact",
                                  metadata={"source": "llm-extracted",
                                            "extracted_at": now_ts})
                personal += 1
                continue
            if await _stage_org_candidate(manager, content, now_ts):
                staged += 1
            continue
        await manager.add(
            content,
            memory_type=mtype,
            metadata={"source": "llm-extracted", "extracted_at": now_ts},
        )
        personal += 1

    if staged:
        log.info("[CONSOLIDATOR] extracted %d personal, staged %d org candidate(s) "
                 "for review", personal, staged)
    else:
        log.info("[CONSOLIDATOR] extracted %d fact/preference entries", personal)
    return personal + staged


async def _stage_org_candidate(manager, content: str, now_ts: str) -> bool:
    """Put an org-relevant extraction into the review queue. Never publishes.

    Automatic extraction is the highest-risk path into shared memory — nobody
    read the conversation it came from, and the detector is young. So the gate's
    verdict decides the *sensitivity label*, not whether to publish: everything
    from this path is staged, per §9.2 of the governance design ("default-deny
    early, loosen as the classifier proves out"). A `normal` verdict here means
    "no findings", not "safe to share".

    Returns False when the fact is already in the collective store, so a weekly
    consolidation pass does not re-stage the same thing for review every week.
    """
    from .. import sensitivity as _sens

    collective = manager.collective_store
    try:
        embedding = await manager._embed(content)
    except Exception as exc:                      # noqa: BLE001
        log.warning("[CONSOLIDATOR] could not embed org candidate: %s", exc)
        return False
    # Same guard manager.add() applies. Writing a wrong-dimension vector into
    # the collective store would make the entry unsearchable rather than fail.
    if not manager._embed_write_ok(embedding):
        return False

    # Dedup against everything already there, whatever its status: re-staging a
    # fact a reviewer already rejected would make the queue an argument.
    if collective.search(embedding, top_k=1, threshold=0.93,
                         include_deprecated=True):
        return False

    verdict = _sens.classify(content)
    collective.add(content, embedding, "fact", metadata={
        "visibility":  "org",
        "sensitivity": verdict["sensitivity"],
        "status":      "candidate",
        "detection":   {"reasons": verdict["reasons"],
                        "injection": verdict["injection"]},
        "provenance":  {"source_type": "extracted",
                        "source_user_id": _owner_of(manager),
                        "source_store": str(manager.store.path),
                        "extracted_at": now_ts},
        "review":      {"approved_by": "", "approved_at": "",
                        "policy_version": "p1"},
    })
    return True


def _owner_of(manager) -> str:
    """Whose conversation this extraction came from.

    MemoryManager carries no user_id, but a per-user store lives at
    memory/users/<uuid>/suni_memory.json, so the owner is the parent directory.
    Deriving it matters: without it an extracted candidate has no attributable
    source, and erasure.py can no more reach it than it can reach the legacy
    unattributed entries — which is the gap this phase exists to stop widening.
    """
    try:
        parent = Path(manager.store.path).resolve().parent
        return parent.name if parent.parent.name == "users" else ""
    except Exception:                             # noqa: BLE001
        return ""


# ── Supersession (semantic contradiction resolution) ─────────────────────────

def _parse_supersede(text: str, n: int) -> list[tuple[int, int]]:
    """Parse 'OLD <i> NEW <j>' lines → validated (old_idx, new_idx) pairs.
    Discards anything malformed, out of range, or where new is not strictly
    later than old (indices are ordered oldest→newest by the caller)."""
    pairs: list[tuple[int, int]] = []
    seen_old: set[int] = set()
    for line in text.splitlines():
        m = re.match(r"^\s*OLD\s+(\d+)\s+NEW\s+(\d+)\s*$", line, re.IGNORECASE)
        if not m:
            continue
        old_i, new_i = int(m.group(1)), int(m.group(2))
        if not (0 <= old_i < n and 0 <= new_i < n):
            continue
        if new_i <= old_i:            # NEW must be the more recent entry
            continue
        if old_i in seen_old:         # never deprecate the same entry twice
            continue
        seen_old.add(old_i)
        pairs.append((old_i, new_i))
    return pairs


async def supersede_facts(
    store,
    host: str = "",
    model: str = LLM_MODEL_FALLBACK,
) -> int:
    """
    Ask the LLM which stored facts contradict/replace earlier facts about the
    same attribute, then soft-deprecate the stale ones (reason=superseded,
    superseded_by=<newer id>). Conservative by construction: the model is told
    to output nothing when unsure, and all actions are reversible.
    Returns the number of entries deprecated.
    """
    active = [e for e in store.get_all()
              if e["type"] in ("fact", "preference") and _active(e)]
    if len(active) < 2:
        return 0

    # oldest → newest so "NEW replaces OLD" maps to increasing index
    active.sort(key=lambda e: e["timestamp"])
    if len(active) > SUPERSEDE_CAP:
        active = active[-SUPERSEDE_CAP:]   # keep the most recent window

    listing = "\n".join(f"{i}: {e['content']}" for i, e in enumerate(active))
    out = await _call_ollama(
        _SUPERSEDE_USER.format(facts=listing), _SUPERSEDE_SYSTEM, host, model
    )

    pairs = _parse_supersede(out, len(active))
    if not pairs:
        return 0

    items = [(active[old_i]["id"], "superseded", active[new_i]["id"])
             for old_i, new_i in pairs]
    deprecated = store.deprecate_many(items)
    if deprecated:
        log.info("[CONSOLIDATOR] superseded %d contradicting fact(s)", deprecated)
    return deprecated


# ── Deduplication (soft) ─────────────────────────────────────────────────────

def _dedup_by_similarity(store, types: tuple[str, ...], threshold: float,
                         reason: str) -> int:
    """Soft-deprecate near-duplicate ACTIVE entries of the given types. For each
    pair with cosine ≥ threshold, the older entry is deprecated with
    superseded_by pointing at the newer one it duplicates. Single atomic save."""
    from .store import _decode, _cosine_batch
    import numpy as np

    candidates = [
        e for e in store.get_all()
        if e["type"] in types and "e16" in e and _active(e)
    ]
    if len(candidates) < 2:
        return 0

    try:
        matrix = np.stack([_decode(e["e16"]) for e in candidates])  # (N, D)
    except Exception as exc:
        log.warning("[CONSOLIDATOR] dedup matrix build failed: %s", exc)
        return 0

    dropped: set[str] = set()
    items: list[tuple[str, str, str]] = []
    for i in range(len(candidates)):
        if candidates[i]["id"] in dropped:
            continue
        scores = _cosine_batch(matrix[i], matrix)
        for j in range(i + 1, len(candidates)):
            if candidates[j]["id"] in dropped:
                continue
            if scores[j] >= threshold:
                # keep the newer entry, deprecate the older
                if candidates[i]["timestamp"] >= candidates[j]["timestamp"]:
                    keep, drop = candidates[i], candidates[j]
                else:
                    keep, drop = candidates[j], candidates[i]
                items.append((drop["id"], reason, keep["id"]))
                dropped.add(drop["id"])
                if drop["id"] == candidates[i]["id"]:
                    break  # i itself was dropped; stop comparing from it
    if not items:
        return 0
    removed = store.deprecate_many(items)
    log.info("[CONSOLIDATOR] deprecated %d near-duplicate %s entries", removed, "/".join(types))
    return removed


def dedup(store) -> int:
    """Soft-deprecate near-duplicate fact/preference entries."""
    threshold = _cfg("memory_dedup_threshold", DEDUP_THRESHOLD)
    return _dedup_by_similarity(store, ("fact", "preference"), threshold, "duplicate")


def dedup_conversations(store) -> int:
    """Soft-deprecate near-duplicate conversation entries."""
    threshold = _cfg("memory_conv_dedup_threshold", CONV_DEDUP_THRESH)
    return _dedup_by_similarity(store, ("conversation",), threshold, "duplicate")


def age_out(store, days: int) -> int:
    """Soft-deprecate conversation entries older than `days`. 0/None disables."""
    if not days or days <= 0:
        return 0
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    items = [
        (e["id"], "aged_out", None)
        for e in store.get_all()
        if e["type"] == "conversation" and _active(e) and e["timestamp"] < cutoff
    ]
    if not items:
        return 0
    removed = store.deprecate_many(items)
    log.info("[CONSOLIDATOR] aged out %d old conversation entries (>%dd)", removed, days)
    return removed


# ── Orchestration ────────────────────────────────────────────────────────────

async def consolidate(
    manager,
    host: str = "",
    model: str = LLM_MODEL_FALLBACK,
    since_ts: str | None = None,
) -> dict:
    """
    Full consolidation pipeline. Acquires a per-store-path lock to prevent
    concurrent passes for the same user. All removals are reversible
    soft-deprecations. Returns a summary dict.
    """
    lock = _get_lock(str(manager.store.path))
    async with lock:
        store = manager.store
        log.info("[CONSOLIDATOR] starting — store=%s", store.path)

        extracted  = await extract_facts(manager, host=host, model=model, since_ts=since_ts)
        superseded = 0
        if _cfg("memory_supersede", True):
            superseded = await supersede_facts(store, host=host, model=model)
        dupes_removed = dedup(store)
        conv_deduped  = dedup_conversations(store)
        aged_out      = age_out(store, _cfg("memory_ageout_days", 0))

        now_ts = datetime.now().isoformat()
        store.set_meta({"last_consolidated": now_ts})
        summary = {
            "extracted":     extracted,
            "superseded":    superseded,
            "dupes_removed": dupes_removed,   # fact/preference duplicates (soft)
            "conv_deduped":  conv_deduped,
            "aged_out":      aged_out,
            "ts":            now_ts,
        }
        log.info("[CONSOLIDATOR] done: %s", summary)
        return summary


# ── Background scheduler ─────────────────────────────────────────────────────

_stop_event: asyncio.Event | None = None


async def start_consolidation_scheduler(
    get_user_managers_fn,
    host: str = "",
    model: str = LLM_MODEL_FALLBACK,
    interval_days: int = 7,
    run_hour: int = 3,
) -> None:
    """Periodic consolidation scheduler. The first pass is aligned to the next
    `run_hour`; subsequent passes run every `interval_days` (weekly by default).
    Runs for every active user manager."""
    global _stop_event
    _stop_event = asyncio.Event()
    first = True

    while not _stop_event.is_set():
        now = datetime.now()
        if first:
            target = now.replace(hour=run_hour, minute=15, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            delay = (target - now).total_seconds()
            first = False
        else:
            delay = max(1, interval_days) * 86400
        log.info("[CONSOLIDATOR] next scheduled pass in %.0fs", delay)

        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

        if _stop_event.is_set():
            break

        managers: dict = get_user_managers_fn()
        for user_id, mgr in list(managers.items()):
            try:
                since_ts = mgr.store.get_meta().get("last_consolidated")
                result   = await consolidate(mgr, host=host, model=model, since_ts=since_ts)
                log.info("[CONSOLIDATOR] user=%s %s", user_id, result)
            except Exception as exc:
                log.error("[CONSOLIDATOR] failed for user=%s: %s", user_id, exc, exc_info=True)


def stop_consolidation_scheduler() -> None:
    if _stop_event:
        _stop_event.set()
