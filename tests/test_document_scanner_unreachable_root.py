"""A root the scanner cannot read must never be mistaken for deleted files.

os.walk() on a missing path yields nothing and raises nothing. Before the fix,
an unmounted drive therefore looked exactly like every indexed file having been
deleted, and scan_once() purged the whole document index — one full index
rewrite per file, blocking the server for hours. Seen for real on 2026-09-15
when a disk move lost a mapped drive.
"""
import asyncio
import os

from suni.ingestion import document_scanner as ds


class FakeStore:
    def __init__(self):
        self.deleted = []
        self.flushes = 0
        self.dirty = False

    def flush(self):
        # The store batches writes now; the scanner flushes it before saving
        # its own state, so a double has to answer this too.
        self.flushes += 1
        return False

    def delete_by_path(self, path):
        self.deleted.append(path)
        return 1

    def add_chunks(self, entries):
        return []

    def file_count(self):
        return 0

    def count(self):
        return 0


def _write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _indexed(path):
    st = os.stat(path)
    return {"mtime": st.st_mtime, "size": st.st_size, "ids": [1], "chunks": 1}


def _ghost():
    return {"mtime": 1.0, "size": 1, "ids": [1], "chunks": 1}


def _scan(monkeypatch, paths, state):
    monkeypatch.setattr(ds, "_load_state", lambda: state)
    monkeypatch.setattr(ds, "_save_state", lambda s: None)
    store = FakeStore()

    async def embed(*a, **k):
        return []

    summary = asyncio.run(ds.scan_once(paths, store, embed))
    return store, summary


def test_unreachable_root_deletes_nothing(monkeypatch, tmp_path):
    gone = str(tmp_path / "unmounted_drive")
    state = {
        os.path.join(gone, "a.txt"): _ghost(),
        os.path.join(gone, "sub", "b.pdf"): _ghost(),
    }
    store, summary = _scan(monkeypatch, [gone], state)
    assert store.deleted == []
    assert summary["files_deleted"] == 0
    assert len(state) == 2, "index entries for an unreachable root must be kept"


def test_a_genuinely_deleted_file_is_still_removed(monkeypatch, tmp_path):
    root = str(tmp_path / "docs")
    keep = _write(os.path.join(root, "keep.txt"))
    removed = os.path.join(root, "removed.txt")
    state = {keep: _indexed(keep), removed: _ghost()}
    store, summary = _scan(monkeypatch, [root], state)
    assert store.deleted == [removed]
    assert summary["files_deleted"] == 1


def test_only_the_reachable_root_can_lose_files(monkeypatch, tmp_path):
    present = str(tmp_path / "present")
    missing = str(tmp_path / "missing")
    kept = _write(os.path.join(present, "kept.txt"))
    removed = os.path.join(present, "removed.txt")
    state = {
        kept: _indexed(kept),
        removed: _ghost(),
        os.path.join(missing, "c.txt"): _ghost(),
    }
    store, _ = _scan(monkeypatch, [present, missing], state)
    assert store.deleted == [removed]


def test_entries_outside_the_configured_roots_are_left_alone(monkeypatch, tmp_path):
    root = str(tmp_path / "docs")
    _write(os.path.join(root, "k.txt"))
    elsewhere = os.path.join(str(tmp_path / "old_folder"), "x.txt")
    state = {elsewhere: _ghost()}
    store, _ = _scan(monkeypatch, [root], state)
    assert store.deleted == []


def test_a_sibling_with_the_same_prefix_is_not_under_the_root(monkeypatch, tmp_path):
    root = str(tmp_path / "data")
    _write(os.path.join(root, "k.txt"))
    sibling = os.path.join(str(tmp_path / "data2"), "x.txt")
    state = {sibling: _ghost()}
    store, _ = _scan(monkeypatch, [root], state)
    assert store.deleted == [], "data2 must not count as being inside data"
