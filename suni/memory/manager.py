"""
MemoryManager: async interface over MemoryStore.

Episodic memory (add/search/build_context) uses nomic-embed-text via Ollama
(768-dim, GPU-accelerated). Document knowledge-base queries stay on
all-MiniLM-L6-v2 (384-dim, CPU) because the FAISS index is fixed at 384-dim.

Memory types:
  conversation  — exchange summaries stored after each turn
  fact          — user-stated facts ("I work at X", "my name is Y")
  preference    — user preferences ("I prefer short answers")
  task          — ongoing task context ("working on project Z")
"""
from __future__ import annotations
import asyncio
import logging
import re
from functools import lru_cache
from .store import MemoryStore

log = logging.getLogger("suni.memory")

_FACT_PATTERNS = [
    r"\bmy name is\b", r"\bi am\b", r"\bi work\b", r"\bi prefer\b",
    r"\bi like\b", r"\bi hate\b", r"\bremember that\b", r"\bdon't forget\b",
    r"\bi'm\b", r"\bmy .+ is\b",
]

EMBED_MODEL_NAME = "all-MiniLM-L6-v2"   # kept for doc_store / FAISS (384-dim)
NOMIC_MODEL      = "nomic-embed-text"    # episodic memory (768-dim)
# Resolved per call, never frozen in a default argument — see config.ollama_host()
def OLLAMA_HOST() -> str:                      # noqa: N802 (kept as the old name)
    from .. import config as _c
    return _c.ollama_host()

# Structured named memories — deliberate, addressable facts the assistant saves
# via the `memory_*` tools. Distinct from auto-derived episodic entries: the
# consolidator only ever touches conversation/fact/preference (positive
# whitelists in every stage), so this type is never deprecated/aged-out by it.
STRUCTURED_TYPE = "structured"
STRUCTURED_CATEGORIES = ("user", "project", "feedback", "reference")


@lru_cache(maxsize=1)
def _get_model_384():
    """384-dim sentence-transformers model for FAISS doc_store queries."""
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(EMBED_MODEL_NAME, device="cpu")


async def embed_nomic(text: str, host: str = "", model: str = NOMIC_MODEL) -> list[float]:
    """Embedding via an Ollama /api/embed endpoint (default: local nomic-embed-text,
    768-dim). host/model are overridable so episodic embeddings can point at a
    remote Ollama without changing the model."""
    import httpx
    host = host or OLLAMA_HOST()
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{host.rstrip('/')}/api/embed",
            json={"model": model, "input": text, "keep_alive": -1},
        )
        r.raise_for_status()
        return r.json()["embeddings"][0]


async def embed_openai(text: str, base_url: str, model: str, api_key: str = "") -> list[float]:
    """Embedding via an OpenAI-compatible /v1/embeddings endpoint (vLLM or other).
    base_url should include the /v1 suffix. Keep the model nomic-dimension-
    compatible unless you re-embed existing memory."""
    import httpx
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{base_url.rstrip('/')}/embeddings",
            json={"model": model, "input": text}, headers=headers,
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]


def _detect_type(text: str) -> str:
    low = text.lower()
    if any(re.search(p, low) for p in _FACT_PATTERNS):
        return "fact"
    if "prefer" in low or "always" in low or "never" in low:
        return "preference"
    return "conversation"


def _clearance_scope(clearance: set | None):
    """
    Build a candidate-stage predicate for the collective store: an entry is
    readable only if it is approved AND its visibility is in the caller's
    clearance. Legacy entries (no governance metadata) default to
    status=approved / visibility=org, so pre-governance data stays visible.
    Fail-safe: a missing clearance is treated as {"org"} (never wide-open).
    """
    allowed = set(clearance) if clearance else {"org"}

    def _pred(entry: dict) -> bool:
        meta = entry.get("metadata") or {}
        if meta.get("status", "approved") != "approved":
            return False
        return meta.get("visibility", "org") in allowed

    return _pred


class MemoryManager:
    def __init__(
        self,
        store_path: str = "memory/suni_memory.json",
        embed_model: str = EMBED_MODEL_NAME,   # kept for API compat, ignored
        doc_store=None,                         # optional DocumentStore (Tier 3)
        collective_store=None,                  # optional separate collective MemoryStore
    ):
        self.store            = MemoryStore(store_path)
        self.doc_store        = doc_store
        self.collective_store = collective_store   # Tier 2b collective episodic
        self.embed_model      = EMBED_MODEL_NAME
        self._embed_dim_warned = False             # dimension-guard: warn once

    async def _embed(self, text: str) -> list[float]:
        """Episodic/collective embedding via the CONFIGURED endpoint. Host-swap is
        safe (same model/dims); a model change needs a re-embed (guarded on write).
        Default = local Ollama nomic-embed-text (768-dim) — unchanged behaviour."""
        from .. import config as _cfg
        backend = _cfg.get("embed_backend", "ollama")
        base    = _cfg.get("embed_base_url", "") or OLLAMA_HOST()
        model   = _cfg.get("embed_model", NOMIC_MODEL)
        if backend == "openai":
            return await embed_openai(text, base, model, _cfg.get("embed_api_key", ""))
        return await embed_nomic(text, host=base, model=model)

    def _embed_write_ok(self, embedding: list[float]) -> bool:
        """Dimension guard: refuse a write whose embedding dim differs from what
        the store already holds — a mis-configured embedder would otherwise mix
        incompatible vectors and silently break recall. Reads are unaffected."""
        store_dim = self.store.vector_dim()
        if store_dim and len(embedding) != store_dim:
            if not self._embed_dim_warned:
                log.error(
                    "[EMBED] configured embedder produces dim=%d but this store holds "
                    "dim=%d — REFUSING memory writes to avoid mixing incompatible "
                    "vectors. This is almost certainly a wrong embed_model/embed_base_url. "
                    "After an INTENTIONAL model change, re-embed with reembed_memory.py.",
                    len(embedding), store_dim,
                )
                self._embed_dim_warned = True
            return False
        return True

    async def _embed_384(self, text: str) -> list[float]:
        """384-dim MiniLM embedding for FAISS doc_store queries only."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: _get_model_384().encode(text).tolist())

    async def add(
        self,
        content: str,
        memory_type: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        mtype = memory_type or _detect_type(content)
        embedding = await self._embed(content)
        if not self._embed_write_ok(embedding):
            return ""   # skipped — dimension mismatch (loud-logged once)
        # Offload the mutation + full-store file rewrite to a worker thread so it
        # never blocks the event loop (this rewrite once stalled the loop → TTS 503s).
        return await asyncio.to_thread(self.store.add, content, embedding, mtype, metadata)

    async def add_exchange(self, user_msg: str, assistant_msg: str) -> None:
        """Store a conversation turn as a single memory."""
        summary = f"User: {user_msg[:200]}\nSuni: {assistant_msg[:300]}"
        await self.add(summary, memory_type="conversation")

        if _detect_type(user_msg) in ("fact", "preference"):
            await self.add(user_msg, memory_type=_detect_type(user_msg))

    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        embedding = await self._embed(query)
        # Numpy cosine scan runs in a thread — keeps the event loop responsive.
        return await asyncio.to_thread(self.store.search, embedding, top_k)

    # ── Structured named memories (model-facing memory_* tools) ───────────────
    async def save_memory(self, name: str, content: str,
                          category: str = "reference", source: str = "assistant") -> dict:
        """Upsert a named memory (identity = name; category is mutable)."""
        name = (name or "").strip()
        if not name:
            raise ValueError("memory name is required")
        if not (content or "").strip():
            raise ValueError("memory content is required")
        cat = category if category in STRUCTURED_CATEGORIES else "reference"
        embedding = await self._embed(content)
        if not self._embed_write_ok(embedding):
            raise ValueError("memory write refused: embedder dimension mismatch (see logs)")
        meta = {"category": cat, "source": source}
        mid, created = self.store.upsert_named(name, content, embedding, STRUCTURED_TYPE, meta)
        return {"action": "created" if created else "updated",
                "name": name.strip().lower(), "category": cat, "id": mid}

    async def search_memory(self, query: str, top_k: int = 5) -> list[dict]:
        embedding = await self._embed(query)
        return await asyncio.to_thread(
            lambda: self.store.search(embedding, top_k=top_k,
                                      memory_type=STRUCTURED_TYPE, threshold=0.2))

    def list_memory(self) -> list[dict]:
        out = []
        for e in self.store.get_by_type(STRUCTURED_TYPE):
            meta = e.get("metadata") or {}
            if meta.get("lifecycle") == "deprecated":
                continue
            out.append({"name": meta.get("name", ""), "category": meta.get("category", "reference"),
                        "content": e["content"], "updated_at": meta.get("updated_at") or e["timestamp"]})
        return sorted(out, key=lambda x: x["updated_at"], reverse=True)

    def delete_memory(self, name: str) -> bool:
        return self.store.deprecate_named(name, STRUCTURED_TYPE, reason="deleted")

    async def build_context(self, query: str, top_k: int | None = None,
                             user_id: str = "", clearance: set | None = None) -> str:
        """
        Return a formatted string of relevant memories (Tier 2) and document
        excerpts (Tier 3) to inject into prompts.

        top_k:     explicit override. When None (the normal path — the
                   orchestrator passes no value) memory and document counts come
                   from config: memory_top_k and doc_top_k respectively.

                   These were previously a single hardcoded default of 5, and
                   nothing read the config keys — so the admin panel's two
                   sliders had no effect, and tuning them down to reduce context
                   bloat silently did nothing.
        user_id:   when set, also returns user-scoped doc_store results tagged
                   as [USER-DOC-EXCERPT-UNTRUSTED] for privacy visibility.
        clearance: set of global-memory visibility labels the caller may read
                   (e.g. {"org"} or {"org","restricted"}). Applied to the
                   *collective* store only — the user's own private store is
                   never scoped. Defaults to {"org"} (fail-safe) when omitted.
        """
        from .. import config as _cfg

        def _count(key: str, fallback: int) -> int:
            """Config value, clamped to something sane. A bad value must not be
            able to inject an unbounded amount of context."""
            if top_k is not None:
                return top_k
            try:
                return max(0, min(int(_cfg.get(key, fallback)), 50))
            except (TypeError, ValueError):
                return fallback

        mem_top_k = _count("memory_top_k", 5)
        doc_top_k = _count("doc_top_k", 5)
        try:
            embedding = await self._embed(query)   # 768-dim: episodic + collective
        except Exception as _e:
            # Embedding backend (Ollama) momentarily unavailable/busy — e.g. right
            # after local image generation freed its VRAM. Skip memory retrieval for
            # this turn rather than crashing the whole request.
            log.warning("[MEMORY] query embed failed (%s) — skipping memory this turn", _e)
            return ""
        lines: list[str] = []
        mem_lines: list[str] = []   # Tier-2 recalled memory — wrapped UNTRUSTED below

        # Structured named memories — deliberate, high-value facts; labeled recall
        # so they surface reliably (not crowded out by conversation summaries).
        # All vector scans below run in worker threads (asyncio.to_thread) so the
        # per-turn retrieval never blocks the event loop, even on a large store.
        structured = await asyncio.to_thread(lambda: self.store.search(
            embedding, top_k=min(mem_top_k, 4),
            memory_type=STRUCTURED_TYPE, threshold=0.25,
        ))
        for m in structured:
            meta = m.get("metadata") or {}
            mem_lines.append(
                f"[STRUCTURED:{meta.get('category', 'reference')}:{meta.get('name', '')}] {m['content']}"
            )

        # Tier 2a — user episodic memories (private; never ACL-scoped). Structured
        # entries get their own labeled recall above, so exclude them here.
        memories = await asyncio.to_thread(lambda: self.store.search(
            embedding, top_k=mem_top_k,
            scope=lambda e: e["type"] != STRUCTURED_TYPE,
        ))
        for m in memories:
            tag = m["type"].upper()
            mem_lines.append(f"[{tag}] {m['content']}")

        # Tier 2b — collective episodic memory (company-level). ACL-scoped by
        # clearance: only approved entries whose visibility the caller may read.
        if self.collective_store:
            coll_mems = await asyncio.to_thread(lambda: self.collective_store.search(
                embedding, top_k=mem_top_k, scope=_clearance_scope(clearance)
            ))
            for m in coll_mems:
                mem_lines.append(f"[COLLECTIVE-{m['type'].upper()}] {m['content']}")

        # Facts/preferences can be LLM-extracted from earlier messages that
        # contained pasted email or web content, so a laundered instruction must
        # not become a trusted command. Wrap recalled memory in UNTRUSTED markers
        # (explained in SUNI_SYSTEM's CONTENT SAFETY section).
        if mem_lines:
            lines.append("[MEMORY-UNTRUSTED]")
            lines.extend(mem_lines)
            lines.append("[/MEMORY-UNTRUSTED]")

        # Tier 3 — document knowledge base
        # H3: excerpts are wrapped with UNTRUSTED markers (prompt injection guard)
        # Gated by config: disabling skips the 384-dim embed + FAISS search entirely
        # (the dominant per-turn retrieval cost when a large doc index is present).
        if _cfg.get("doc_kb_enabled", True) and self.doc_store and self.doc_store.count() > 0:
            embedding_384 = await self._embed_384(query)  # lazy: only when doc_store has data
            scope_filter = ["collective"]
            if user_id:
                scope_filter.append(f"user:{user_id}")
            doc_results = await asyncio.to_thread(lambda: self.doc_store.search(
                embedding_384, top_k=doc_top_k, scope_filter=scope_filter
            ))
            for d in doc_results:
                src       = f"{d['file_name']} p.{d['page']}"
                is_user   = d.get("scope", "collective").startswith("user:")
                tag_open  = "[USER-DOC-EXCERPT-UNTRUSTED:" if is_user else "[DOC-EXCERPT-UNTRUSTED:"
                tag_close = "[/USER-DOC-EXCERPT-UNTRUSTED]" if is_user else "[/DOC-EXCERPT-UNTRUSTED]"
                lines.append(
                    f"{tag_open}{src}]\n"
                    f"{d['excerpt']}\n"
                    f"{tag_close}"
                )

        if not lines:
            return ""

        return (
            "--- Relevant memories ---\n"
            + "\n".join(lines)
            + "\n--- End memories ---"
        )

    def embed_batch_sync(self, texts: list[str]) -> list[list[float]]:
        """Synchronous batch embed — for use in thread executor by scanner (384-dim)."""
        model = _get_model_384()
        return model.encode(texts, batch_size=32).tolist()

    def stats(self) -> dict:
        recent = self.store.get_recent(3)
        doc_stats = self.doc_store.stats() if self.doc_store else {}
        life = self.store.lifecycle_counts()
        return {
            "total":      self.store.count(),   # raw total, includes deprecated
            "active":     life["active"],
            "deprecated": life["deprecated"],
            "recent":     [r["content"][:60] for r in recent],
            "doc_store":  doc_stats,
        }
