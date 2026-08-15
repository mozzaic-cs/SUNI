"""
Document scanner — real-time file system monitoring for configured folders.

Architecture:
  1. Startup: initial full scan (catches anything changed while SUNI was offline).
  2. Watchdog: OS-level kernel notifications (ReadDirectoryChangesW on Windows).
     Zero polling, zero CPU between events.
  3. Debounce: 2-second delay per file before processing (prevents mid-write reads).
  4. Safety rescan: every 24 hours, full delta scan to recover from missed events
     (e.g. network share disconnection).

GPU embedding:
  Chat-time embedding (_get_model in manager.py) always runs on CPU to avoid VRAM
  contention with qwen2.5:7b. Document ingestion uses GPU when qwen is idle
  (not loaded in VRAM) and enough VRAM is free, falling back to CPU otherwise.
  GPU gives ~30x speedup: 0.001s vs 0.03s per chunk — matters for large corpora.
"""
from __future__ import annotations
import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from ..logger import get_logger
from ..memory.document_store import DocumentStore
from .document_loader import SUPPORTED_EXTENSIONS, load
from .document_chunker import chunk_pages
from ..system_profile import EMBED_BATCH_SIZE, INGEST_CONCURRENCY, SAFETY_RESCAN_S

_log = get_logger(__name__)

_STATE_PATH    = Path("memory/doc_scan.json")
_DEBOUNCE_S    = 2.0             # seconds to wait after last event before processing
_SAFETY_RESCAN = SAFETY_RESCAN_S # from system profile (24 h)
_BATCH_EMBED   = EMBED_BATCH_SIZE # from system profile (RAM-derived)


# ── State persistence ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        import json
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    import json
    tmp = _STATE_PATH.with_suffix(".tmp")
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_STATE_PATH)


# ── GPU / device selection ────────────────────────────────────────────────────

async def _ingestion_device() -> str:
    """
    Use GPU for ingestion if qwen is not loaded and enough VRAM is free.
    Falls back to CPU. Chat-time embedding is always CPU (see manager.py).
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2) as c:
            from .. import config as _c
            r = await c.get(f"{_c.ollama_host()}/api/ps")
            if r.json().get("models"):
                _log.debug("[SCAN] qwen loaded — using CPU for embedding")
                return "cpu"
    except Exception:
        pass

    try:
        import torch
        if torch.cuda.is_available():
            free_gb = torch.cuda.mem_get_info()[0] / 1024 ** 3
            if free_gb >= 0.5:
                _log.info("[SCAN] qwen idle, %.1f GB VRAM free — using GPU for embedding", free_gb)
                return "cuda"
    except Exception:
        pass

    return "cpu"


def _build_embed_fn(device: str) -> Callable:
    """Return a synchronous batch embed function for the given device."""
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("all-MiniLM-L6-v2", device=device)

    def _embed(texts: list[str]) -> list[list[float]]:
        return model.encode(texts, batch_size=_BATCH_EMBED,
                            show_progress_bar=False).tolist()

    return _embed


# ── File ingestion ────────────────────────────────────────────────────────────

async def _ingest_file(
    file_path: str,
    mtime: float,
    size: int,
    doc_store: DocumentStore,
    embed_fn: Callable,
    state: dict,
) -> int:
    _log.info("[SCAN] ingesting %s", file_path)
    loop = asyncio.get_event_loop()

    pages = await loop.run_in_executor(None, load, file_path)
    if not pages:
        return 0

    chunks = chunk_pages(pages)
    if not chunks:
        return 0

    file_type = Path(file_path).suffix.lower().lstrip(".")
    texts = [c["text"] for c in chunks]

    # Embed in executor (may use GPU — keeps event loop free)
    embeddings: list[list[float]] = []
    for i in range(0, len(texts), _BATCH_EMBED):
        batch = texts[i: i + _BATCH_EMBED]
        vecs  = await loop.run_in_executor(None, embed_fn, batch)
        embeddings.extend(vecs)

    # Detect scope: user uploads are in memory/uploads/{user_id}/
    import re as _re
    _scope = "collective"
    _uid_m = _re.search(r"memory[/\\]uploads[/\\]([^/\\]+)[/\\]", file_path)
    if _uid_m:
        _scope = f"user:{_uid_m.group(1)}"

    entries = [
        {
            "embedding":   emb,
            "file_path":   file_path,
            "file_type":   file_type,
            "mtime":       mtime,
            "page":        c["page"],
            "chunk_index": c["chunk_index"],
            "excerpt":     c["excerpt"],
            "scope":       _scope,
        }
        for c, emb in zip(chunks, embeddings)
    ]

    ids = doc_store.add_chunks(entries)
    state[file_path] = {"mtime": mtime, "size": size, "ids": ids, "chunks": len(ids)}
    return len(ids)


async def _remove_file(file_path: str, doc_store: DocumentStore, state: dict) -> int:
    n = doc_store.delete_by_path(file_path)
    state.pop(file_path, None)
    if n:
        _log.info("[SCAN] removed %d chunks for %s", n, file_path)
    return n


# ── Full delta scan ───────────────────────────────────────────────────────────

async def scan_once(
    paths: list[str],
    doc_store: DocumentStore,
    embed_fn: Callable,
) -> dict:
    """Walk all paths, compare to saved state, ingest/remove deltas."""
    state = _load_state()

    on_disk: dict[str, tuple[float, int]] = {}
    for root in paths:
        try:
            for dirpath, _dirs, files in os.walk(root):
                for fname in files:
                    fp  = os.path.join(dirpath, fname)
                    ext = Path(fp).suffix.lower()
                    if ext not in SUPPORTED_EXTENSIONS:
                        continue
                    try:
                        st = os.stat(fp)
                        on_disk[fp] = (st.st_mtime, st.st_size)
                    except OSError:
                        pass
        except Exception as e:
            _log.warning("[SCAN] walk failed for %s: %s", root, e)

    new_paths = [p for p in on_disk if p not in state]
    modified  = [
        p for p in on_disk
        if p in state and (
            on_disk[p][0] != state[p].get("mtime")
            or on_disk[p][1] != state[p].get("size")
        )
    ]
    deleted   = [p for p in state if p not in on_disk]

    chunks_added = chunks_modified = 0

    for fp in deleted + modified:
        await _remove_file(fp, doc_store, state)

    # Ingest new + modified files with bounded concurrency
    sem = asyncio.Semaphore(INGEST_CONCURRENCY)

    async def _bounded_ingest(fp: str, mtime: float, size: int) -> int:
        async with sem:
            return await _ingest_file(fp, mtime, size, doc_store, embed_fn, state)

    newly_ingested: list[str] = []   # track for E5 rollback
    tasks = {
        fp: asyncio.create_task(_bounded_ingest(fp, *on_disk[fp]))
        for fp in new_paths + modified
    }
    for fp, task in tasks.items():
        try:
            n = await task
            if n > 0:
                newly_ingested.append(fp)
            if fp in new_paths:
                chunks_added += n
            else:
                chunks_modified += n
        except Exception as e:
            _log.error("[SCAN] ingest error %s: %s", fp, e, exc_info=True)

    # E5: if state save fails, roll back all newly added chunks to prevent duplicates
    try:
        _save_state(state)
    except Exception as e:
        _log.error("[SCAN] state save failed — rolling back %d ingested files: %s",
                   len(newly_ingested), e)
        for fp in newly_ingested:
            doc_store.delete_by_path(fp)
            state.pop(fp, None)
        raise
    summary = {
        "files_new": len(new_paths), "files_modified": len(modified),
        "files_deleted": len(deleted),
        "chunks_added": chunks_added, "chunks_modified": chunks_modified,
        "total_files": doc_store.file_count(), "total_chunks": doc_store.count(),
    }
    _log.info("[SCAN] done: +%d new, ~%d modified, -%d deleted",
              len(new_paths), len(modified), len(deleted))
    return summary


# ── Watchdog bridge ───────────────────────────────────────────────────────────

class _EventBridge(FileSystemEventHandler):
    """Translates watchdog thread events → asyncio queue entries."""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self._loop  = loop
        self._queue = queue

    def _put(self, action: str, path: str) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (action, path))

    def on_created(self, event):
        if not event.is_directory and Path(event.src_path).suffix.lower() in SUPPORTED_EXTENSIONS:
            self._put("add", event.src_path)

    def on_modified(self, event):
        if not event.is_directory and Path(event.src_path).suffix.lower() in SUPPORTED_EXTENSIONS:
            self._put("modify", event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._put("delete", event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._put("delete", event.src_path)
            if Path(event.dest_path).suffix.lower() in SUPPORTED_EXTENSIONS:
                self._put("add", event.dest_path)


# ── Debounced event processor ─────────────────────────────────────────────────

async def _process_events(
    queue: asyncio.Queue,
    doc_store: DocumentStore,
    embed_fn: Callable,
    stop_event: asyncio.Event,
) -> None:
    """
    Drain the event queue with per-file debouncing.
    Pending tasks are cancelled and restarted if the same file fires again
    within DEBOUNCE_S seconds — prevents processing files mid-write.
    """
    pending: dict[str, asyncio.Task] = {}
    state = _load_state()

    async def _handle(action: str, path: str) -> None:
        await asyncio.sleep(_DEBOUNCE_S)
        try:
            if action == "delete":
                await _remove_file(path, doc_store, state)
            else:
                try:
                    st = os.stat(path)
                    mtime, size = st.st_mtime, st.st_size
                except OSError:
                    return
                if path in state and (
                    state[path].get("mtime") == mtime
                    and state[path].get("size") == size
                ):
                    return  # unchanged
                if path in state:
                    await _remove_file(path, doc_store, state)
                await _ingest_file(path, mtime, size, doc_store, embed_fn, state)
            _save_state(state)
        except Exception as e:
            _log.error("[WATCH] handler error %s: %s", path, e, exc_info=True)
        finally:
            pending.pop(path, None)

    while not stop_event.is_set():
        try:
            action, path = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        # Cancel existing debounce task for this file and restart
        if path in pending:
            pending[path].cancel()
        pending[path] = asyncio.create_task(_handle(action, path))


# ── Main watcher ──────────────────────────────────────────────────────────────

_last_scan:      dict  = {}
_last_scan_time: float = 0.0
_scan_running:   bool  = False


async def watch_documents(
    doc_store: DocumentStore,
    embed_fn: Callable,           # CPU embed fn from MemoryManager (fallback)
    stop_event: asyncio.Event,
    get_paths: Callable,
    get_interval: Callable,       # kept for API compat; ignored by real-time watcher
) -> None:
    global _last_scan, _last_scan_time, _scan_running

    paths = get_paths()
    if not paths:
        _log.info("[SCAN] no paths configured — watcher idle")
        # Wait until stop, re-check periodically in case config changes
        while not stop_event.is_set():
            await asyncio.sleep(30)
            paths = get_paths()
            if paths:
                break
        if not paths:
            return

    # Determine embedding device (GPU if qwen idle, else CPU)
    device = await _ingestion_device()
    loop   = asyncio.get_event_loop()
    ingest_embed = await loop.run_in_executor(None, _build_embed_fn, device)
    _log.info("[SCAN] embedding device: %s", device)

    # ── 1. Initial full scan ──────────────────────────────────────────────
    _log.info("[SCAN] starting initial full scan of %d path(s)", len(paths))
    _scan_running = True
    try:
        _last_scan      = await scan_once(paths, doc_store, ingest_embed)
        _last_scan_time = time.time()
    except Exception as e:
        _log.error("[SCAN] initial scan failed: %s", e, exc_info=True)
    finally:
        _scan_running = False

    # E8: release GPU embed model after bulk initial scan to free VRAM for qwen
    if device == "cuda":
        try:
            import torch as _torch
            del ingest_embed
            _torch.cuda.empty_cache()
            _log.info("[SCAN] GPU embed model released after initial scan")
        except Exception:
            pass
        # Rebuild with CPU for ongoing real-time events (qwen likely active now)
        ingest_embed = await loop.run_in_executor(None, _build_embed_fn, "cpu")
        _log.info("[SCAN] switched to CPU embedding for real-time events")

    # ── 2. Start watchdog observer ────────────────────────────────────────
    event_queue: asyncio.Queue = asyncio.Queue()
    bridge   = _EventBridge(loop, event_queue)
    observer = Observer()
    for p in paths:
        try:
            observer.schedule(bridge, p, recursive=True)
            _log.info("[SCAN] watching %s (real-time)", p)
        except Exception as e:
            _log.warning("[SCAN] cannot watch %s: %s", p, e)
    observer.start()

    # ── 3. Start event processor + safety rescan loop ─────────────────────
    processor = asyncio.create_task(
        _process_events(event_queue, doc_store, ingest_embed, stop_event)
    )

    try:
        last_safety = time.time()
        while not stop_event.is_set():
            await asyncio.sleep(60)
            # Safety rescan every 24h (catches missed events, e.g. network drops)
            if time.time() - last_safety >= _SAFETY_RESCAN:
                _log.info("[SCAN] running 24h safety rescan")
                _scan_running = True
                try:
                    _last_scan      = await scan_once(paths, doc_store, ingest_embed)
                    _last_scan_time = time.time()
                except Exception as e:
                    _log.error("[SCAN] safety rescan failed: %s", e, exc_info=True)
                finally:
                    _scan_running = False
                last_safety = time.time()
    finally:
        processor.cancel()
        observer.stop()
        observer.join(timeout=5)
        _log.info("[SCAN] watcher stopped")


def scanner_status() -> dict:
    return {
        "running":        _scan_running,
        "last_scan_time": _last_scan_time,
        "last_scan":      _last_scan,
        "mode":           "realtime+watchdog",
    }
