"""Embedding must not build a new HTTP client for every call.

Measured 2026-09-26: 873 ms per embedding with a client per call, 99 ms with one
kept open — and both the SSL context and the fresh TCP connection are built ON the
event loop. Ingesting a transcript embeds thousands of chunks in a row, which left
the loop with no room to serve a chat turn: a background assignment sat at 0%
progress for eight minutes while the loop was busy constructing clients.
"""
import asyncio

import httpx
import pytest

from suni.memory import manager


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _no_cached_clients():
    manager._HTTP_CLIENTS.clear()
    yield
    manager._HTTP_CLIENTS.clear()


def test_the_client_is_reused_within_a_loop():
    async def go():
        return manager._http() is manager._http()
    assert asyncio.run(go()) is True


def test_each_loop_gets_its_own_client():
    """A client is bound to the loop it was made on; sharing one across loops
    fails at runtime, and the CLI, the tests and the server do not share one.

    The clients are held, not compared by id(): ids are recycled once an object
    is collected, which is exactly the trap the loop-keyed cache itself fell into.
    """
    held = []

    async def go():
        held.append(manager._http())

    asyncio.run(go())
    asyncio.run(go())
    assert held[0] is not held[1]


def test_embedding_twice_constructs_one_client(monkeypatch):
    built = []

    class _Counting:
        """Standalone, not a subclass: another test in this suite leaves a fake
        on httpx.AsyncClient whose is_closed is true, and inheriting from it made
        the cache replace the client every call — the very thing under test."""
        is_closed = False

        def __init__(self, *a, **kw):
            built.append(1)

        async def post(self, *a, **kw):
            return _FakeResponse({"embeddings": [[0.1] * 768]})

    monkeypatch.setattr(httpx, "AsyncClient", _Counting)

    async def go():
        await manager.embed_nomic("one")
        await manager.embed_nomic("two")

    asyncio.run(go())
    assert len(built) == 1, f"built {len(built)} clients for two embeddings"


def test_the_embedding_still_comes_back(monkeypatch):
    class _Fake:
        is_closed = False

        def __init__(self, *a, **kw):
            pass

        async def post(self, *a, **kw):
            return _FakeResponse({"embeddings": [[0.25] * 768]})

    monkeypatch.setattr(httpx, "AsyncClient", _Fake)
    got = asyncio.run(manager.embed_nomic("text"))
    assert len(got) == 768 and got[0] == 0.25


def test_a_closed_client_is_replaced_rather_than_reused():
    async def go():
        first = manager._http()
        await first.aclose()
        second = manager._http()
        return first is second, second.is_closed
    same, closed = asyncio.run(go())
    assert same is False and closed is False
