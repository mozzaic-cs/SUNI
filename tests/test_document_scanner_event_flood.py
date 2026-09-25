"""A flood of watcher events must neither re-read known files nor pin the loop.

2026-09-21: something touched every file under a watched folder tree and the
watcher got ~70k events. Two things then held a chat turn for minutes:
  * files that yield no text (images without OCR) were never recorded in the
    state, so every event on one looked like a NEW file and was loaded again;
  * every handled event re-serialised the whole ~2 MB state ON the event loop.
"""
import asyncio
import os

from suni.ingestion import document_scanner as ds


class FakeStore:
    dirty = False

    def delete_by_path(self, path):
        return 0

    def add_chunks(self, entries):
        return list(range(len(entries)))

    def flush(self):
        # The store batches its writes; the watcher flushes it before saving
        # scanner state, so the double answers this too.
        return False


def _write(path, text="x"):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_textless_file_is_remembered(monkeypatch, tmp_path):
    img = _write(str(tmp_path / "logo.jpg"))
    st = os.stat(img)
    monkeypatch.setattr(ds, "load", lambda p: [])     # e.g. no OCR installed
    state = {}
    n = asyncio.run(ds._ingest_file(img, st.st_mtime, st.st_size,
                                    FakeStore(), lambda t: [], state))
    assert n == 0
    assert state[img] == {"mtime": st.st_mtime, "size": st.st_size,
                          "ids": [], "chunks": 0}


def test_flood_loads_each_file_once_and_saves_coalesced(monkeypatch, tmp_path):
    files = [_write(str(tmp_path / f"img{i}.jpg")) for i in range(40)]
    loads, saves = [], []
    monkeypatch.setattr(ds, "load", lambda p: loads.append(p) or [])
    monkeypatch.setattr(ds, "_load_state", lambda: {})
    monkeypatch.setattr(ds, "_save_state", lambda s: saves.append(len(s)))
    monkeypatch.setattr(ds, "_DEBOUNCE_S", 0.01)
    monkeypatch.setattr(ds, "_SAVE_EVERY_S", 0.2)

    async def run():
        q, stop = asyncio.Queue(), asyncio.Event()
        proc = asyncio.create_task(
            ds._process_events(q, FakeStore(), lambda t: [], stop))
        for _round in range(3):              # the same files, three times over
            for f in files:
                q.put_nowait(("modify", f))
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)
        stop.set()
        await asyncio.wait_for(proc, 3)

    asyncio.run(run())
    assert sorted(loads) == sorted(files), "each unchanged file is read once"
    assert 1 <= len(saves) <= 5, f"saves must be coalesced, got {len(saves)}"
    assert saves[-1] == len(files)
