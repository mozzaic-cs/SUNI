"""
FAISS-backed document knowledge store — Tier 3 memory.

Default index: IndexIDMap2(IndexFlatIP) — exact search, sub-ms at scale.

Compression (A4): call compress() to migrate to IVF-PQ.
  IVF-PQ parameters (for D=384):
    nlist = 256  Voronoi cells  (≈ sqrt(81 000))
    M     = 48   sub-quantizers (= D / 8)
    nbits = 8    bits per code
  Vector memory: 384 × 4 B = 1 536 B → 48 B  (32× compression)
  Recall@10 with nprobe=32: ~97% of exact

Files:
  memory/doc_index.faiss   — FAISS binary index (FlatIP or IVF-PQ)
  memory/doc_meta.json     — metadata list (compact JSON, no indent)
  memory/doc_scan.json     — scanner state {path -> {mtime, size, ids}}
"""
from __future__ import annotations
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import faiss
import numpy as np

_DIM    = 384   # all-MiniLM-L6-v2 output dimension
_log    = logging.getLogger("suni.memory.docstore")

# IVF-PQ defaults (A4)
_PQ_NLIST  = 256   # Voronoi cells
_PQ_M      = 48    # sub-quantizers  (D must be divisible by M → 384/48=8 ✓)
_PQ_NBITS  = 8     # bits per code
_PQ_NPROBE = 32    # cells to visit at search time (accuracy vs speed)
_PQ_MIN_TRAIN = _PQ_NLIST * 40   # minimum vectors needed to train (10 240)


class DocumentStore:

    def __init__(self, index_path: str, meta_path: str):
        self.index_path = Path(index_path)
        self.meta_path  = Path(meta_path)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)

        self._meta: dict[int, dict] = {}    # vector_id → metadata
        self._next_id: int = 0
        self._index: faiss.Index = self._make_flat_index()
        self._load()

    # ── Index factories ────────────────────────────────────────────────────

    @staticmethod
    def _make_flat_index() -> faiss.Index:
        """Default: exact inner-product search."""
        base = faiss.IndexFlatIP(_DIM)
        return faiss.IndexIDMap2(base)

    @staticmethod
    def _make_ivfpq_index() -> faiss.Index:
        """Approximate: IVF-PQ — 32× smaller, ~97% recall@10 with nprobe=32."""
        quantizer = faiss.IndexFlatIP(_DIM)
        ivfpq     = faiss.IndexIVFPQ(quantizer, _DIM, _PQ_NLIST, _PQ_M, _PQ_NBITS,
                                     faiss.METRIC_INNER_PRODUCT)
        ivfpq.nprobe = _PQ_NPROBE
        return faiss.IndexIDMap2(ivfpq)

    def index_type(self) -> str:
        """Return a human-readable index type string."""
        inner = faiss.downcast_index(
            faiss.downcast_index(self._index).index
            if hasattr(faiss.downcast_index(self._index), 'index')
            else self._index
        )
        name = type(inner).__name__
        if "IVFPQ" in name:
            return f"IVF-PQ (nlist={_PQ_NLIST}, M={_PQ_M}, nbits={_PQ_NBITS})"
        return "FlatIP (exact)"

    # ── Persistence ───────────────────────────────────────────────────────

    def _load(self) -> None:
        if self.index_path.exists():
            try:
                self._index = faiss.read_index(str(self.index_path))
            except Exception:
                bak = self.index_path.with_suffix(".bak")
                if bak.exists():
                    try:
                        self._index = faiss.read_index(str(bak))
                    except Exception:
                        self._index = self._make_flat_index()
                else:
                    self._index = self._make_flat_index()

        if self.meta_path.exists():
            try:
                raw = json.loads(self.meta_path.read_text(encoding="utf-8"))
                # raw is a list; convert to {id: meta} dict
                self._meta = {int(entry["_vid"]): entry for entry in raw}
                if self._meta:
                    self._next_id = max(self._meta.keys()) + 1
            except Exception:
                self._meta = {}

    def _save(self) -> None:
        # Backup existing index before overwriting (E10 — recovery on corrupt write)
        if self.index_path.exists():
            import shutil as _shutil
            _shutil.copy2(str(self.index_path), str(self.index_path.with_suffix(".bak")))
        faiss.write_index(self._index, str(self.index_path))
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(list(self._meta.values()), ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.meta_path)

    # ── Write ─────────────────────────────────────────────────────────────

    def add_chunks(self, chunks: list[dict]) -> list[int]:
        """
        Add a batch of chunks. Each chunk dict must have:
          embedding (list[float]), file_path, file_type, mtime,
          page (int), chunk_index (int), excerpt (str)
        Returns the list of assigned vector IDs.
        """
        if not chunks:
            return []

        vecs, ids = [], []
        for chunk in chunks:
            vid = self._next_id
            self._next_id += 1

            vec = np.asarray(chunk["embedding"], dtype=np.float32)
            faiss.normalize_L2(vec.reshape(1, -1))
            vecs.append(vec)
            ids.append(vid)

            self._meta[vid] = {
                "_vid":        vid,
                "file_path":   chunk["file_path"],
                "file_name":   Path(chunk["file_path"]).name,
                "file_type":   chunk["file_type"],
                "mtime":       chunk["mtime"],
                "page":        chunk.get("page", 0),
                "chunk_index": chunk["chunk_index"],
                "excerpt":     chunk["excerpt"],   # already sentence-boundary trimmed by chunker (A5)
                "indexed_at":  datetime.now().isoformat(),
                # scope: "collective" (company-wide) or "user:{user_id}" (private)
                # Missing scope on old entries defaults to "collective" at search time.
                "scope":       chunk.get("scope", "collective"),
            }

        mat   = np.stack(vecs).astype(np.float32)
        id_arr = np.array(ids, dtype=np.int64)
        self._index.add_with_ids(mat, id_arr)
        self._save()
        return ids

    def delete_by_path(self, file_path: str) -> int:
        """Remove all chunks belonging to file_path. Returns count removed."""
        vids = [
            vid for vid, m in self._meta.items()
            if m["file_path"] == file_path
        ]
        if not vids:
            return 0
        self._index.remove_ids(np.array(vids, dtype=np.int64))
        for vid in vids:
            del self._meta[vid]
        self._save()
        return len(vids)

    # ── Search ────────────────────────────────────────────────────────────

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        threshold: float = 0.30,
        scope_filter: list[str] | None = None,
    ) -> list[dict]:
        """Return top-k metadata dicts with scores, filtered by threshold.

        scope_filter: if set, only return chunks whose scope is in this list.
        Chunks without a scope field are treated as "collective".
        None = no filter (returns all scopes — backward compatible).
        """
        if self._index.ntotal == 0:
            return []

        q = np.asarray(query_embedding, dtype=np.float32).reshape(1, -1)
        faiss.normalize_L2(q)
        # Search more candidates when filtering by scope so we get enough after filtering
        k = min(top_k * (3 if scope_filter else 1), self._index.ntotal)
        scores, ids = self._index.search(q, k)

        results = []
        for score, vid in zip(scores[0], ids[0]):
            if vid < 0 or float(score) < threshold:
                continue
            meta = self._meta.get(int(vid))
            if not meta:
                continue
            if scope_filter is not None:
                chunk_scope = meta.get("scope", "collective")
                if chunk_scope not in scope_filter:
                    continue
            results.append({**meta, "score": float(score)})
            if len(results) >= top_k:
                break
        return results

    # ── Compression (A4 — IVF-PQ) ────────────────────────────────────────

    def compress(self, nlist: int = _PQ_NLIST, m: int = _PQ_M,
                 nbits: int = _PQ_NBITS) -> dict:
        """
        Migrate the current index to IVF-PQ in-place.

        Requires at least nlist * 40 vectors for training (≥10 240 for nlist=256).
        On success: saves compressed index, returns stats dict.
        On failure: original index is unchanged (backup preserved).

        Memory reduction: 32× for default parameters (384-dim, M=48, nbits=8).
        Search recall@10: ~97% with nprobe=32.
        """
        n = self._index.ntotal
        if n < nlist * 40:
            return {
                "ok": False,
                "error": f"Need at least {nlist * 40:,} vectors to train IVF-PQ "
                         f"(have {n:,}). Lower nlist or add more documents.",
            }

        _log.info("[COMPRESS-A4] Extracting %d vectors for IVF-PQ training…", n)

        # Reconstruct all vectors from the current index
        all_ids  = np.array(list(self._meta.keys()), dtype=np.int64)
        vecs     = np.zeros((len(all_ids), _DIM), dtype=np.float32)
        try:
            self._index.reconstruct_batch(all_ids, vecs)
        except Exception as e:
            return {"ok": False, "error": f"Vector reconstruction failed: {e}"}

        # Build and train the new IVF-PQ index
        quantizer = faiss.IndexFlatIP(_DIM)
        ivfpq     = faiss.IndexIVFPQ(quantizer, _DIM, nlist, m, nbits,
                                     faiss.METRIC_INNER_PRODUCT)
        ivfpq.nprobe = _PQ_NPROBE
        new_index = faiss.IndexIDMap2(ivfpq)

        _log.info("[COMPRESS-A4] Training IVF-PQ (nlist=%d, M=%d, nbits=%d)…",
                  nlist, m, nbits)
        ivfpq.train(vecs)
        new_index.add_with_ids(vecs, all_ids)

        # Swap and persist (backup of original made by _save)
        self._index = new_index
        self._save()

        size_before = _DIM * 4 * n        # float32 FlatIP bytes (approx)
        size_after  = m * (nbits // 8) * n  # PQ bytes (approx)
        _log.info("[COMPRESS-A4] Done. ~%dx compression (%d → %d bytes approx)",
                  size_before // max(size_after, 1), size_before, size_after)

        return {
            "ok":           True,
            "vectors":      n,
            "index_type":   self.index_type(),
            "bytes_before": size_before,
            "bytes_after":  size_after,
            "ratio":        round(size_before / max(size_after, 1), 1),
        }

    # ── Stats ─────────────────────────────────────────────────────────────

    def count(self) -> int:
        return int(self._index.ntotal)

    def file_count(self) -> int:
        return len({m["file_path"] for m in self._meta.values()})

    def stats(self) -> dict:
        types: dict[str, int] = {}
        for m in self._meta.values():
            types[m["file_type"]] = types.get(m["file_type"], 0) + 1
        return {
            "total_chunks": self.count(),
            "total_files":  self.file_count(),
            "index_type":   self.index_type(),
            "by_type":      types,
        }
