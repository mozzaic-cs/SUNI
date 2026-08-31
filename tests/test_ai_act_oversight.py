"""
The two Art 14/Art 12 controls added for SMB deployment, tested at the seam that
actually matters.

Both of these are exactly the shape of failure this project has shipped before:
a setting that reaches nothing. `record_model()` existing and being callable
proves nothing — the question is whether a real request leaves a real audit row
with the model named in it, and whether a real stop actually halts a real run.
So the assertions here are on the AUDIT ROW and on the SSE STREAM, never on the
helper functions in isolation.

  Art 12  — "which model produced this output" must be answerable from the log.
  Art 14(4)(e) — a human must be able to interrupt a run in progress.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

from suni import audit as _audit
from suni import usage as _usage
from tests.conftest import parse_sse

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Captured at COLLECTION time, before conftest's session-scoped fixture patches
# `Orchestrator._safe_run` with a stub. Reaching for the attribute inside the
# test would hand back that stub, and the test would prove the mock is
# cancellable — which is exactly the gap it exists to close.
from suni.core.orchestrator import Orchestrator as _Orchestrator
_REAL_SAFE_RUN = _Orchestrator._safe_run
assert _REAL_SAFE_RUN.__name__ == "_safe_run", (
    "the real _safe_run was already patched at import time")


# ── Art 12: the model that answered is on the row ────────────────────────────

def test_the_audit_row_names_the_model_that_answered(client, std_headers, monkeypatch):
    """End-to-end: a run that calls a model must leave that model on its audit
    row. Asserting on record_model() alone would pass with the column never
    written, which is the bug class this guards."""
    async def _run(self, user_input, context, **kwargs):
        _usage.record_model("qwen2.5:7b")
        _usage.record_model("qwen2.5:32b")     # a mid-run tier escalation
        return "ok"

    monkeypatch.setattr("suni.core.orchestrator.Orchestrator._safe_run", _run)
    r = client.post("/api/chat", headers=std_headers,
                    json={"message": "hello", "session_id": "art12-models"})
    assert r.status_code == 200

    rows, _ = _audit.query(limit=20, route="chat")
    row = next((x for x in rows if x["session_id"] == "art12-models"), None)
    assert row is not None, "the request left no audit row at all"
    assert "models" in row, "audit_log has no models column"
    assert row["models"] == "qwen2.5:7b,qwen2.5:32b", (
        f"escalation not recorded in call order: {row['models']!r}")


def test_the_models_column_survives_into_the_csv_export(client, std_headers, monkeypatch):
    """The Audit tab and the CSV both read query(); a column that reaches the
    table but not the export is invisible to the person doing the review."""
    async def _run(self, user_input, context, **kwargs):
        _usage.record_model("csv-probe-model")
        return "ok"

    monkeypatch.setattr("suni.core.orchestrator.Orchestrator._safe_run", _run)
    client.post("/api/chat", headers=std_headers,
                json={"message": "hi", "session_id": "art12-csv"})

    csv_text = _audit.export_csv()
    assert "models" in csv_text.splitlines()[0], "models missing from CSV header"
    assert "csv-probe-model" in csv_text


def test_a_request_that_calls_no_model_records_an_empty_string(client, std_headers):
    """Absence must read as absence. A NULL here would crash the CSV writer and
    an invented default would be a false record."""
    r = client.post("/api/chat", headers=std_headers,
                    json={"message": "hi", "session_id": "art12-empty"})
    assert r.status_code == 200
    rows, _ = _audit.query(limit=20, route="chat")
    row = next((x for x in rows if x["session_id"] == "art12-empty"), None)
    assert row is not None and row["models"] == ""


def test_every_inference_path_records_the_model_it_used():
    """A new agent added upstream that never calls record_model() would silently
    produce unattributable audit rows. This fails the build instead."""
    paths = {
        "suni/models/ollama_agent.py",
        "suni/models/openai_agent.py",
        "suni/models/claude_agent.py",
        "suni/models/claude_code_agent.py",
        "suni/models/codex_agent.py",
        "suni/vision.py",
    }
    for rel in sorted(paths):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "record_model(" in src, f"{rel} answers without naming its model"


def test_the_model_list_is_bounded():
    """An unbounded loop must not write an unbounded audit cell."""
    acc, tok = _usage.start()
    try:
        for i in range(200):
            _usage.record_model(f"m{i}")
        assert len(_usage.models_used()) <= _usage._MAX_MODELS
    finally:
        _usage.reset(tok)


def test_record_model_outside_a_request_is_a_no_op():
    """Non-chat entrypoints bind no accumulator; this must not raise."""
    _usage.record_model("orphan")          # must not raise
    assert _usage.models_used() == []


# ── Art 14(4)(e): a human can interrupt a run ────────────────────────────────

def test_stopping_with_no_run_in_progress_is_answered_not_errored(client, std_headers):
    r = client.post("/api/chat/stop", headers=std_headers,
                    json={"session_id": "nothing-here"})
    assert r.status_code == 200
    assert r.json()["stopped"] is False


async def test_stop_actually_halts_an_in_flight_run(app, client, std_headers,
                                                   monkeypatch):
    """The real thing: a long run, a stop from the user, and a stream that ends
    with the interruption recorded rather than the answer.

    Driven through an in-loop AsyncClient rather than two threads on the sync
    TestClient: that client serialises requests through one blocking portal, so
    the stop would queue behind the very run it is meant to cancel and the test
    would hang instead of failing.
    """
    import httpx

    started = asyncio.Event()

    async def _slow(self, user_input, context, **kwargs):
        started.set()
        await asyncio.sleep(30)            # far longer than the test will wait
        return "should never be reached"

    monkeypatch.setattr("suni.core.orchestrator.Orchestrator._safe_run", _slow)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as ac:
        chat = asyncio.create_task(ac.post(
            "/api/chat", headers=std_headers,
            json={"message": "long one", "session_id": "art14-stop"}))
        await asyncio.wait_for(started.wait(), timeout=10)
        await asyncio.sleep(0.3)           # let the handler register the run

        stop = await ac.post("/api/chat/stop", headers=std_headers,
                             json={"session_id": "art14-stop"})
        assert stop.status_code == 200
        assert stop.json()["stopped"] is True, "the run was not found to stop"

        r = await asyncio.wait_for(chat, timeout=15)

    kinds = [e.get("type") for e in parse_sse(r.content)]
    assert "stopped" in kinds, f"client never saw a stopped event: {kinds}"
    assert "done" in kinds, "the stream did not close cleanly after the stop"

    # The interruption is itself an oversight record — Art 14 is only evidenced
    # if the intervention appears in the trail.
    rows, _ = _audit.query(limit=50)
    assert any(r["route"] == "chat.stopped" for r in rows),         "a human halted the system and the audit trail does not show it"


async def test_a_stopped_run_still_leaves_a_request_row(app, client, std_headers,
                                                        monkeypatch):
    """What ran before the interruption is evidence too — Art 12 does not stop
    applying because the run was cut short."""
    import httpx

    started = asyncio.Event()

    async def _slow(self, user_input, context, **kwargs):
        _usage.record_model("halted-model")
        started.set()
        await asyncio.sleep(30)
        return "x"

    monkeypatch.setattr("suni.core.orchestrator.Orchestrator._safe_run", _slow)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as ac:
        chat = asyncio.create_task(ac.post(
            "/api/chat", headers=std_headers,
            json={"message": "cut short", "session_id": "art14-row"}))
        await asyncio.wait_for(started.wait(), timeout=10)
        await asyncio.sleep(0.3)
        await ac.post("/api/chat/stop", headers=std_headers,
                      json={"session_id": "art14-row"})
        await asyncio.wait_for(chat, timeout=15)

    rows, _ = _audit.query(limit=50, route="chat")
    row = next((x for x in rows if x["session_id"] == "art14-row"), None)
    assert row is not None, "an interrupted run left no request row"
    assert row["models"] == "halted-model",         "the model that ran before the stop was not recorded"


async def test_the_real_safe_run_does_not_swallow_the_cancellation():
    """The stop tests above patch `_safe_run` out, so they prove the plumbing
    and NOT that the thing being cancelled is cancellable.

    `_safe_run` is the likeliest place in the tree to eat a cancellation: it
    exists to catch what `run()` throws. It catches `Exception`, and
    `CancelledError` is a `BaseException`, so it propagates — but that is a
    one-word difference between a working stop button and one that silently
    returns a normal answer while the audit row claims the request completed.
    This pins it.
    """
    orch = _Orchestrator.__new__(_Orchestrator)    # no model/registry needed
    orch.memory = "sentinel-memory"

    entered = asyncio.Event()

    async def _never_finishes(*a, **kw):
        entered.set()
        await asyncio.sleep(30)
        return "should never be reached"

    orch.run = _never_finishes

    task = asyncio.create_task(_REAL_SAFE_RUN(orch, "hello", None))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert orch.memory == "sentinel-memory",         "the finally block did not restore memory on cancellation"


def test_the_run_registry_is_cleaned_up_by_identity(client, std_headers):
    """Two requests can share "{user_id}:{session_id}" — ui.html and face.html
    read the session id from localStorage, so a second tab reuses it. Cleanup
    must remove THIS request's entry, never whatever is under the key, or a
    finishing run deregisters a newer one and leaves it unstoppable."""
    src = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    assert "if _active_runs.get(_run_key) is _run_entry:" in src,         "cleanup pops by key rather than by identity"


def test_the_run_registry_does_not_leak(client, std_headers):
    """A registry entry outliving its request would let a later stop cancel a
    task that no longer exists, or pin memory per conversation."""
    from suni.web import server as _srv
    src = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    # The pop must be in the finally block, not on the success path only.
    tail = src.split("finally:")[-1]
    assert "_active_runs.pop" in src
    assert any("_active_runs.pop" in blk for blk in src.split("finally:")[1:]), \
        "the run registry is not cleaned up in a finally"


async def test_one_user_cannot_stop_another_users_run(app, client, std_headers,
                                                     ro_headers, monkeypatch):
    """Stop is user-scoped, the same way approvals are."""
    import httpx

    started = asyncio.Event()

    async def _slow(self, user_input, context, **kwargs):
        started.set()
        await asyncio.sleep(20)
        return "x"

    monkeypatch.setattr("suni.core.orchestrator.Orchestrator._safe_run", _slow)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as ac:
        chat = asyncio.create_task(ac.post(
            "/api/chat", headers=std_headers,
            json={"message": "mine", "session_id": "art14-scope"}))
        await asyncio.wait_for(started.wait(), timeout=10)
        await asyncio.sleep(0.3)

        other = await ac.post("/api/chat/stop", headers=ro_headers,
                              json={"session_id": "art14-scope"})
        assert other.json()["stopped"] is False,             "a user stopped someone else's run"

        # Clean up: the owner stops their own run.
        mine = await ac.post("/api/chat/stop", headers=std_headers,
                             json={"session_id": "art14-scope"})
        assert mine.json()["stopped"] is True
        await asyncio.wait_for(chat, timeout=15)


def test_a_client_disconnect_is_not_reported_as_a_stop():
    """Both a disconnect and a stop arrive as CancelledError. Treating the first
    as the second would swallow a real disconnect and keep streaming into a dead
    socket, so the handler must re-raise when no stop was requested."""
    src = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    block = src.split("except asyncio.CancelledError:")[1].split("_was_stopped")[0]
    assert "stopped_by" in block and "raise" in block, \
        "the handler does not distinguish a disconnect from a deliberate stop"


# ── the reply the user sees ──────────────────────────────────────────────────

def test_the_stop_notice_is_translated():
    """Composed in Python, so it falls under the canned-reply language rule."""
    from suni.core import orchestrator as orch
    assert orch._say("stopped_by_user", "pt-PT")
    assert orch._say("stopped_by_user", "pt-PT") != orch._say("stopped_by_user", "en")
    assert orch._say("stopped_by_user", "ja-JP") == orch._say("stopped_by_user", "en")


def test_the_stop_notice_does_not_claim_the_work_was_undone():
    """Stop halts the run; it does not reverse a sent email. A notice implying
    otherwise would be the most misleading string in the product."""
    from suni.core import orchestrator as orch
    for lang in ("en", "pt-PT", "pt-BR", "es-ES"):
        text = orch._say("stopped_by_user", lang).lower()
        assert text, f"{lang} has no stop notice"
        assert re.search(r"não foi desfeito|no se deshizo|not undone", text), \
            f"{lang} stop notice does not say the completed work stands: {text!r}"


def test_all_three_uis_have_the_stop_control():
    """By this project's own standard (docs/eu-ai-act.md): a control that covers
    one code path is not a control. All three UIs POST /api/chat."""
    for rel in ("suni/web/chat.html", "suni/web/ui.html", "suni/web/face.html"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "/api/chat/stop" in src, f"{rel} can start a run it cannot stop"
        assert "chat.stop" in src, f"{rel} has no translated stop label"


def test_the_stop_button_is_not_rate_limited():
    """A safety control that answers 429 is not a safety control."""
    src = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    head = src.split('@app.post("/api/chat/stop")')[1].split("async def")[0]
    assert "_limiter.limit" not in head, "the stop endpoint is rate limited"
