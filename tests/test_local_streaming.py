"""The local model's reply appears while it is being written — carefully.

At ~18 tok/s a 300-token answer is seventeen seconds of nothing, which is what a
pinned local agent showed. Two things make naive streaming wrong here, and both
are tested:

  * a turn that calls a tool must not have its text forwarded — the user would
    watch a preamble that the answer then replaces, and the speech queue reads
    whatever the token stream gives it;
  * an escalatable turn can be retried at a higher tier, so its words may be
    discarded. Only turns nothing can replace are shown live.
"""
import asyncio
import types

import pytest

from suni.core.orchestrator import may_stream_tokens
from suni.models.ollama_agent import OllamaAgent


# ── the gate ─────────────────────────────────────────────────────────────────
def gate(**over):
    base = dict(has_live_consumer=True, pinned=False, current_tier=2,
                max_local_tier=3, t5_available=True, agent_streams=True)
    base.update(over)
    return may_stream_tokens(**base)


def test_a_pinned_model_streams():
    assert gate(pinned=True) is True


def test_an_escalatable_turn_does_not_stream():
    # Tier 2 of 3 with Claude Code behind it: this answer can still be replaced.
    assert gate() is False


def test_the_last_resort_tier_streams_because_nothing_follows_it():
    assert gate(current_tier=3, max_local_tier=3, t5_available=False) is True
    assert gate(current_tier=3, max_local_tier=3, t5_available=True) is False


def test_nothing_streams_without_a_live_consumer_or_a_streaming_backend():
    assert gate(pinned=True, has_live_consumer=False) is False
    assert gate(pinned=True, agent_streams=False) is False


# ── the reader ───────────────────────────────────────────────────────────────
def _chunk(content="", tool_calls=None, **extra):
    msg = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(message=msg, **extra)


class _FakeClient:
    def __init__(self, chunks):
        self._chunks = chunks
        self.kwargs = None

    async def chat(self, **kwargs):
        self.kwargs = kwargs

        async def gen():
            for c in self._chunks:
                yield c
        return gen()


def _agent(chunks):
    a = OllamaAgent.__new__(OllamaAgent)       # no client/config construction
    a.client = _FakeClient(chunks)
    return a


def _run(agent, seen):
    return asyncio.run(agent._chat_streaming({"model": "m", "messages": []}, seen.append))


def test_text_is_forwarded_as_it_arrives_and_assembled_whole():
    seen = []
    out = _run(_agent([_chunk("Bom "), _chunk("dia"), _chunk(".", eval_count=3)]), seen)
    assert seen == ["Bom ", "dia", "."], "each piece should reach the caller as it lands"
    assert out.message.content == "Bom dia."


def test_the_final_chunks_statistics_survive():
    # The trace note and telemetry are built from these; a streamed turn that
    # lost them would silently stop reporting tokens per second.
    out = _run(_agent([_chunk("hi"), _chunk("", eval_count=42, prompt_eval_count=7)]), [])
    assert out.eval_count == 42 and out.prompt_eval_count == 7


def test_a_tool_call_turn_forwards_nothing():
    tc = object()
    seen = []
    out = _run(_agent([_chunk("", tool_calls=[tc]), _chunk("")]), seen)
    assert seen == [], "a tool-call turn must not put text on screen"
    assert out.message.tool_calls == [tc]


def test_forwarding_stops_for_good_once_a_tool_call_appears():
    tc = object()
    seen = []
    _run(_agent([_chunk("Vou verificar"), _chunk("", tool_calls=[tc]),
                 _chunk("mais texto")]), seen)
    assert seen == ["Vou verificar"], "text after a tool call must not be forwarded"


def test_a_display_failure_never_breaks_the_run():
    def boom(_):
        raise RuntimeError("the browser went away")
    out = asyncio.run(_agent([_chunk("a"), _chunk("b")])._chat_streaming(
        {"model": "m", "messages": []}, boom))
    assert out.message.content == "ab"


def test_an_empty_stream_is_an_error_not_an_empty_answer():
    with pytest.raises(RuntimeError):
        _run(_agent([]), [])


def test_streaming_is_requested_from_the_server():
    agent = _agent([_chunk("x")])
    _run(agent, [])
    assert agent.client.kwargs.get("stream") is True
