"""
A chunk that is already stored is not embedded again, and is not counted.

The session watcher re-reads a growing Claude Code session every minute. Every
chunk it had already stored was embedded again, on the same model server as the
chat, only for the store to drop it as an exact duplicate; and the log said
"+67 memories" each minute while the store did not grow by a byte (measured
2026-10-05). The dedup has to happen BEFORE the embedding, and the count has to
be what was actually added.
"""
from __future__ import annotations
import asyncio

import pytest

from suni.ingestion import claude_code as cc
from suni.memory.manager import MemoryManager


@pytest.fixture
def manager(tmp_path, monkeypatch):
    m = MemoryManager(store_path=str(tmp_path / "mem.json"))
    calls = []

    async def fake_embed(text):
        calls.append(text)
        return [0.1, 0.2, 0.3, 0.4]

    monkeypatch.setattr(m, "_embed", fake_embed)
    m.embed_calls = calls
    return m


def test_adding_known_content_does_not_embed_it_again(manager):
    first = asyncio.run(manager.add("the same exchange", memory_type="conversation"))
    again = asyncio.run(manager.add("the same exchange", memory_type="conversation"))
    assert first and again == first
    assert manager.embed_calls == ["the same exchange"], "a duplicate was embedded"


def test_a_re_read_session_counts_only_what_is_new(manager, tmp_path, monkeypatch):
    chunks = [{"content": f"pair {i}", "project": "p", "session": "s"} for i in range(5)]
    session = {"path": str(tmp_path / "s.jsonl"), "mtime": 1, "project": "p", "session_id": "s"}
    monkeypatch.setattr(cc, "_load_state", lambda: {})
    monkeypatch.setattr(cc, "_save_state", lambda state: None)
    monkeypatch.setattr(cc, "find_new_or_updated", lambda state: [session])
    monkeypatch.setattr(cc, "extract_messages", lambda path: ["m"])
    monkeypatch.setattr(cc, "chunk_into_memories", lambda m, p, s: list(chunks))

    first = asyncio.run(cc.ingest_all(manager))
    assert (first["chunks"], first["known"]) == (5, 0)

    chunks.append({"content": "pair 5", "project": "p", "session": "s"})   # the session grew
    second = asyncio.run(cc.ingest_all(manager))
    assert (second["chunks"], second["known"]) == (1, 5)
    assert len(manager.embed_calls) == 6, "already-stored chunks were embedded again"
