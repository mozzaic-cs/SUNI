"""Indexing a file must not rewrite the whole index, and order matters.

One write is the entire FAISS index plus the entire metadata file — measured at
1.5s for 77k chunks — and it used to run once per ingested file, from inside the
async ingest path. A scan of a large tree therefore spent minutes of event-loop
time rewriting what it had just written, while chat waited.

The ordering test is the one that protects data: the scanner records the vector
ids the store handed it, so the index must reach disk BEFORE that state does.
"""
import asyncio
import json

import numpy as np
import pytest

from suni.memory.document_store import DocumentStore
from suni.ingestion import document_scanner as ds


def _chunk(path, i=0):
    return {"embedding": list(np.random.rand(384).astype(np.float32)),
            "file_path": path, "file_type": "txt", "mtime": 1.0,
            "page": 1, "chunk_index": i, "excerpt": "x"}


def _store(tmp_path):
    return DocumentStore(str(tmp_path / "doc_index.faiss"), str(tmp_path / "doc_meta.json"))


def test_adding_a_file_does_not_touch_the_disk(tmp_path):
    st = _store(tmp_path)
    st.add_chunks([_chunk("a.txt")])
    assert not (tmp_path / "doc_index.faiss").exists(), "write must be deferred"
    assert st.dirty
    assert st.flush() is True
    assert (tmp_path / "doc_index.faiss").exists()
    assert json.loads((tmp_path / "doc_meta.json").read_text(encoding="utf-8"))


def test_flush_is_a_no_op_when_nothing_changed(tmp_path):
    st = _store(tmp_path)
    st.add_chunks([_chunk("a.txt")])
    st.flush()
    mtime = (tmp_path / "doc_index.faiss").stat().st_mtime_ns
    assert st.flush() is False, "a clean store must not rewrite the index"
    assert (tmp_path / "doc_index.faiss").stat().st_mtime_ns == mtime


def test_a_long_run_still_reaches_disk_on_its_own(tmp_path, monkeypatch):
    # Deferral is bounded: a bulk scan cannot lose everything on a crash.
    monkeypatch.setattr("suni.memory.document_store._FLUSH_EVERY_N", 3)
    st = _store(tmp_path)
    for i in range(2):
        st.add_chunks([_chunk(f"f{i}.txt")])
    assert not (tmp_path / "doc_index.faiss").exists()
    st.add_chunks([_chunk("f2.txt")])
    assert (tmp_path / "doc_index.faiss").exists(), "must auto-write at the cap"
    assert not st.dirty


def test_what_was_flushed_survives_a_reload(tmp_path):
    st = _store(tmp_path)
    st.add_chunks([_chunk("keep.txt"), _chunk("keep.txt", 1)])
    st.flush()
    again = _store(tmp_path)
    assert again.count() == 2
    assert again.file_count() == 1


def test_scan_writes_the_index_before_the_state_that_names_its_ids(tmp_path, monkeypatch):
    order = []

    class Store:
        dirty = True
        def add_chunks(self, entries):
            return [1]
        def delete_by_path(self, p):
            return 0
        def flush(self):
            order.append("index")
            return True
        def file_count(self):
            return 1
        def count(self):
            return 1

    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(ds, "_load_state", lambda: {})
    monkeypatch.setattr(ds, "_save_state", lambda s: order.append("state"))
    monkeypatch.setattr(ds, "load", lambda p: [{"page": 1, "text": "hello"}])

    asyncio.run(ds.scan_once([str(tmp_path)], Store(), lambda texts: [[0.0] * 384]))
    assert order == ["index", "state"], (
        "state saved first would claim ids the index never persisted")
