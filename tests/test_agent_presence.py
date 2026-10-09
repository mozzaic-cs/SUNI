"""The Face shows a figure for each agent at work; it needs to be told.

One event from one place — Orchestrator.run under an agent profile — so no way
of calling an agent can forget to announce it. The case that matters most is
the delegated sub-run: it is given no event stream on purpose (a specialist's
tokens must not stream as SUNI's), so its figure has to reach the OUTER turn's
stream without anything else from the sub-run going there.
"""
import asyncio

import pytest

from suni import approval
from suni.core import agent_presence
from suni.core.context import Context
from suni.core.orchestrator import Orchestrator
from suni.tools.registry import ToolRegistry

REV = {"slug": "revisor", "name": "Revisor"}
RES = {"slug": "researcher", "name": "Researcher"}


class _Model:
    name = "m"


def _orch(inner):
    o = Orchestrator(_Model(), ToolRegistry())
    o._run_inner = inner
    return o


def _agent_events(events):
    return [(e["phase"], e["slug"]) for e in events if e.get("type") == "agent"]


def test_a_turn_run_as_an_agent_announces_start_and_done():
    async def inner(*a):
        return "ok"
    events = []
    out = asyncio.run(_orch(inner).run("x", Context(), event_cb=events.append,
                                       agent_profile=REV))
    assert out == "ok"
    assert _agent_events(events) == [("start", "revisor"), ("done", "revisor")]
    assert events[0]["name"] == "Revisor"


def test_a_plain_turn_announces_nothing():
    async def inner(*a):
        return "ok"
    events = []
    asyncio.run(_orch(inner).run("x", Context(), event_cb=events.append))
    assert _agent_events(events) == []


def test_a_delegated_sub_run_reaches_the_outer_stream_and_nothing_else_does():
    holder = {}

    async def inner(user_input, context, user_role, conv_mode, response_language,
                    user_mcp_servers, event_cb, *rest):
        if user_input == "outer":
            # As invoke_agent and the fan-out do: no event_cb for the sub-run.
            return await holder["o"].run("inner", Context(), agent_profile=RES)
        # The sub-run itself must not have been handed the outer stream.
        assert event_cb is None
        return "sub"

    events = []
    holder["o"] = _orch(inner)
    asyncio.run(holder["o"].run("outer", Context(), event_cb=events.append))
    assert _agent_events(events) == [("start", "researcher"), ("done", "researcher")]
    assert all(e.get("type") == "agent" for e in events)


def test_a_failed_or_stopped_agent_does_not_stay_on_screen():
    for exc in (RuntimeError("boom"), asyncio.CancelledError()):
        async def inner(*a, _e=exc):
            raise _e
        events = []
        with pytest.raises(type(exc)):
            asyncio.run(_orch(inner).run("x", Context(), event_cb=events.append,
                                         agent_profile=REV))
        assert _agent_events(events) == [("start", "revisor"), ("error", "revisor")]


def test_the_current_agent_is_set_inside_and_restored_after():
    seen = []

    async def inner(*a):
        seen.append(agent_presence.CURRENT_AGENT.get())
        return "ok"
    asyncio.run(_orch(inner).run("x", Context(), event_cb=lambda e: None,
                                 agent_profile=REV))
    assert seen == ["revisor"]
    assert agent_presence.CURRENT_AGENT.get() == ""


def test_an_approval_card_says_which_agent_is_asking(monkeypatch):
    monkeypatch.setattr(approval, "APPROVAL_TIMEOUT", 0.01)
    monkeypatch.setattr(approval, "_audit_log", lambda *a, **k: None)
    events = []
    tok = agent_presence.CURRENT_AGENT.set("revisor")
    try:
        asyncio.run(approval.request_approval("send_email", {"to": "a@b.c"},
                                              user_id="u1", event_cb=events.append))
    finally:
        agent_presence.CURRENT_AGENT.reset(tok)
    card = next(e for e in events if e["type"] == "approval_request")
    assert card["agent"] == "revisor"


def test_a_broken_stream_never_breaks_the_turn():
    def bad(evt):
        raise RuntimeError("socket gone")
    agent_presence.emit(bad, "start", REV)      # must not raise
