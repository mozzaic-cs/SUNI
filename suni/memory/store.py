"""
JSON-backed vector store with float16 embedding quantisation.

Storage format: embeddings are base64-encoded float16 numpy arrays
(stored under key "e16"), giving ~4x size reduction vs float32 JSON lists
with negligible cosine-similarity accuracy loss (<0.1%).

Backward-compatible: entries using the old "embedding" (float32 list) key
are transparently read and converted on next save.

Cosine similarity uses numpy for ~50x speedup over the pure-Python loop.
"""
from __future__ import annotations
import base64
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np


def _encode(vec: list[float] | np.ndarray) -> str:
    """float32 list → base64-encoded float16 bytes."""
    arr = np.asarray(vec, dtype=np.float16)
    return base64.b64encode(arr.tobytes()).decode("ascii")


def _decode(b64: str) -> np.ndarray:
    """base64 float16 bytes → float32 numpy array."""
    raw = base64.b64decode(b64)
    return np.frombuffer(raw, dtype=np.float16).astype(np.float32)


def _cosine_batch(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Vectorised cosine similarity: query (D,) vs matrix (N, D) → (N,)."""
    norms = np.linalg.norm(matrix, axis=1) * np.linalg.norm(query)
    with np.errstate(divide="ignore", invalid="ignore"):
        sims = np.where(norms > 0, matrix @ query / norms, 0.0)
    return sims


class MemoryStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: list[dict[str, Any]] = []
        self._content_ids: dict[str, str] = {}   # content → id, for exact-content dedup
        self._migrated = False
        # Guards _data + the file rewrite. add()/search() are offloaded to worker
        # threads (asyncio.to_thread) so they never block the event loop; this lock
        # serialises them across threads so a concurrent add can't corrupt a scan.
        self._lock = threading.Lock()
        self._load()

    # ──────────────────────────────────────────────────────────────────
    # Persistence
    # ──────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return

        # Detect old format (float32 list in "embedding") and migrate
        needs_save = False
        for entry in raw:
            if "embedding" in entry and "e16" not in entry:
                entry["e16"] = _encode(entry.pop("embedding"))
                needs_save = True
        self._data = raw
        self._reindex()
        if needs_save:
            self._migrated = True
            self._save()

    def _reindex(self) -> None:
        """Rebuild the content→id map used for exact-content dedup on add()."""
        self._content_ids = {e["content"]: e["id"]
                             for e in self._data if e.get("content") is not None}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(self.path)  # atomic rename

    # ──────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────

    def add(
        self,
        content: str,
        embedding: list[float],
        memory_type: str = "conversation",
        metadata: dict | None = None,
    ) -> str:
        # Exact-content dedup: the session watcher re-runs ingest on every mtime
        # bump, so an unchanged (or slowly-growing) session would re-add all its
        # exchanges each poll — this is what ballooned the store to 340MB / 114k
        # rows (97% duplicates). If we already hold this exact content, return the
        # existing id instead of appending a duplicate.
        with self._lock:
            existing = self._content_ids.get(content)
            if existing is not None:
                return existing
            entry = {
                "id":        str(uuid.uuid4()),
                "content":   content,
                "e16":       _encode(embedding),       # float16, compact
                "type":      memory_type,
                "timestamp": datetime.now().isoformat(),
                "metadata":  metadata or {},
            }
            self._data.append(entry)
            self._content_ids[content] = entry["id"]
            self._save()
            return entry["id"]

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        threshold: float = 0.35,
        memory_type: str | None = None,
        scope=None,
        include_deprecated: bool = False,
    ) -> list[dict]:
        # scope: optional predicate entry->bool applied at the CANDIDATE stage,
        # before ranking/top-k. This is deliberate: filtering the returned top-k
        # instead would silently drop in-scope entries whenever out-of-scope
        # entries rank higher (an ACL correctness bug). Default None = no filter,
        # so per-user private stores are unaffected.
        # Snapshot the entry list under the lock so a concurrent add() (running in
        # another worker thread) can't mutate it mid-scan. Entry dicts are shared
        # by reference, but membership is frozen for this search.
        with self._lock:
            candidates = list(self._data)
        if memory_type:
            candidates = [e for e in candidates if e["type"] == memory_type]
        if scope is not None:
            candidates = [e for e in candidates if scope(e)]
        # Deprecated (soft-deleted / superseded) entries are excluded from recall
        # by default so build_context never surfaces stale or contradicted memory.
        # The 'lifecycle' key is deliberately distinct from the collective store's
        # governance 'status' key (approved|pending) to avoid cross-store collision.
        if not include_deprecated:
            candidates = [e for e in candidates
                          if (e.get("metadata") or {}).get("lifecycle") != "deprecated"]

        if not candidates:
            return []

        q = np.asarray(query_embedding, dtype=np.float32)
        q_dim = len(q)

        # Build matrix of embeddings that match query dimension
        valid, entries = [], []
        for e in candidates:
            try:
                vec = _decode(e["e16"]) if "e16" in e else np.asarray(e.get("embedding", []), dtype=np.float32)
                if len(vec) == q_dim:
                    valid.append(vec)
                    entries.append(e)
            except Exception:
                continue

        if not valid:
            return []

        matrix = np.stack(valid)                       # (N, D)
        scores = _cosine_batch(q, matrix)              # (N,)
        mask   = scores >= threshold
        if not mask.any():
            return []

        top_idx = np.argsort(-scores[mask])[:top_k]
        masked_entries = [entries[i] for i in np.where(mask)[0]]
        return [masked_entries[i] for i in top_idx]

    def upsert_named(
        self,
        name: str,
        content: str,
        embedding: list[float],
        memory_type: str,
        metadata: dict | None = None,
    ) -> tuple[str, bool]:
        """
        Insert or update a NAMED memory, keyed by normalized `name` within a type
        (identity = name; category and other metadata are mutable attributes). If
        an active entry with the same (type, name) exists it is updated in place;
        otherwise a new one is created. Returns (id, created).
        """
        key = name.strip().lower()
        now = datetime.now().isoformat()
        for e in self._data:
            meta = e.get("metadata") or {}
            if (e["type"] == memory_type
                    and meta.get("name") == key
                    and meta.get("lifecycle") != "deprecated"):
                e["content"]   = content
                e["e16"]       = _encode(embedding)
                e["timestamp"] = now
                merged = {**meta, **(metadata or {}), "name": key, "updated_at": now}
                e["metadata"] = merged
                self._save()
                return e["id"], False
        entry = {
            "id":        str(uuid.uuid4()),
            "content":   content,
            "e16":       _encode(embedding),
            "type":      memory_type,
            "timestamp": now,
            "metadata":  {**(metadata or {}), "name": key, "created_at": now},
        }
        self._data.append(entry)
        self._save()
        return entry["id"], True

    def get_named(self, name: str, memory_type: str) -> dict | None:
        """Return the active named entry for (type, name), or None."""
        key = name.strip().lower()
        for e in self._data:
            meta = e.get("metadata") or {}
            if (e["type"] == memory_type and meta.get("name") == key
                    and meta.get("lifecycle") != "deprecated"):
                return e
        return None

    def deprecate_named(self, name: str, memory_type: str,
                        reason: str = "deleted") -> bool:
        """Soft-delete a named entry by (type, name). Returns True if one was found."""
        entry = self.get_named(name, memory_type)
        if entry is None:
            return False
        return self.deprecate(entry["id"], reason=reason)

    def get_recent(self, n: int = 10) -> list[dict]:
        return sorted(self._data, key=lambda e: e["timestamp"], reverse=True)[:n]

    def delete(self, memory_id: str) -> bool:
        before = len(self._data)
        self._data = [e for e in self._data if e["id"] != memory_id]
        if len(self._data) < before:
            self._reindex()
            self._save()
            return True
        return False

    def deprecate(self, memory_id: str, reason: str = "deprecated",
                  superseded_by: str | None = None) -> bool:
        """Soft-delete a single entry: mark it deprecated (reversible) instead of
        removing it. Deprecated entries are excluded from search() by default but
        remain on disk for audit/rollback. Returns True if an entry was updated."""
        return self.deprecate_many([(memory_id, reason, superseded_by)]) > 0

    def deprecate_many(self, items) -> int:
        """Bulk soft-delete. items: iterable of (memory_id, reason, superseded_by).
        Mutates all matched entries then performs a single atomic save. Entries
        already deprecated are skipped. Returns the count newly deprecated."""
        index = {e["id"]: e for e in self._data}
        now = datetime.now().isoformat()
        changed = 0
        for memory_id, reason, superseded_by in items:
            entry = index.get(memory_id)
            if entry is None:
                continue
            meta = entry.setdefault("metadata", {})
            if meta.get("lifecycle") == "deprecated":
                continue
            meta["lifecycle"] = "deprecated"
            meta["deprecated_at"] = now
            meta["deprecated_reason"] = reason
            if superseded_by:
                meta["superseded_by"] = superseded_by
            changed += 1
        if changed:
            self._save()
        return changed

    def lifecycle_counts(self) -> dict:
        """Count active vs deprecated entries (overall and by type) — for stats.
        count() still reports the raw total including deprecated entries."""
        active_by_type: dict[str, int] = {}
        active = deprecated = 0
        for e in self._data:
            if (e.get("metadata") or {}).get("lifecycle") == "deprecated":
                deprecated += 1
            else:
                active += 1
                active_by_type[e["type"]] = active_by_type.get(e["type"], 0) + 1
        return {"active": active, "deprecated": deprecated, "active_by_type": active_by_type}

    def get_all(self) -> list[dict]:
        """Shallow copy of all entries (safe for iteration while store mutates)."""
        return list(self._data)

    def get_by_type(self, memory_type: str) -> list[dict]:
        return [e for e in self._data if e["type"] == memory_type]

    def remove_ids(self, ids: set[str]) -> int:
        """Bulk remove entries by ID set. Returns count removed."""
        before = len(self._data)
        self._data = [e for e in self._data if e["id"] not in ids]
        removed = before - len(self._data)
        if removed:
            self._reindex()
            self._save()
        return removed

    def get_meta(self) -> dict:
        """Read sidecar metadata file (last_consolidated, etc.)."""
        meta_path = self.path.with_suffix(".meta.json")
        if not meta_path.exists():
            return {}
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def set_meta(self, data: dict) -> None:
        """Merge-update sidecar metadata file."""
        meta_path = self.path.with_suffix(".meta.json")
        existing = self.get_meta()
        existing.update(data)
        meta_path.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")

    def vector_dim(self) -> int | None:
        """Dimension of the stored embeddings (from the first decodable entry),
        or None if the store is empty. Used by the embedding dimension guard to
        detect a mis-configured embedder before it corrupts recall."""
        for e in self._data:
            b64 = e.get("e16")
            if b64:
                try:
                    return len(_decode(b64))
                except Exception:
                    continue
        return None

    def count(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        self._data = []
        self._content_ids = {}
        self._save()
