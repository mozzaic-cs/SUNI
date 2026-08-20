"""
invoke_agent: delegation that cannot become an escalation or a loop.

Two properties carry the weight.

An agent's grants must intersect against the grants ALREADY IN FORCE for the
calling turn, not against the caller's role. If a narrow agent delegates and the
sub-turn re-derives from the role, the narrowing evaporates — a restricted agent
would be a route back to everything its user can do, which is precisely the
property tests/test_agents.py exists to protect.

And agents must not invoke agents. A -> B -> A is an unbounded loop holding a
GPU and a token budget.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from suni.tools import agent_tool


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    from suni import agents
    monkeypatch.setattr(agents, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(agents, "AGENTS_DB", tmp_path / "agents.db")
    monkeypatch.setattr(agent_tool, "_orchestrator", object())   # bound but unused
    yield


def _run(coro):
    return asyncio.run(coro)


# ── depth cap ────────────────────────────────────────────────────────────────
def test_an_agent_cannot_invoke_another_agent():
    tok = agent_tool.AGENT_DEPTH.set(1)
    try:
        out = _run(agent_tool.handler(agent="anything", task="do a thing"))
    finally:
        agent_tool.AGENT_DEPTH.reset(tok)
    assert "cannot invoke other agents" in out
    assert "depth limit" in out


def test_a_human_turn_may_delegate():
    """Depth 0 must get past the cap and fail on the NAME, not the depth."""
    out = _run(agent_tool.handler(agent="nope", task="x"))
    assert "depth limit" not in out
    assert "no agent called" in out


# ── unknown names fail loudly ────────────────────────────────────────────────
def test_unknown_agent_is_refused_not_silently_self_handled():
    out = _run(agent_tool.handler(agent="Mark", task="check the network"))
    assert "no agent called 'Mark'" in out
    assert "list_agents" in out
    assert "do not substitute your own work" in out


def test_ambiguous_names_ask_rather_than_guess():
    from suni import agents
    agents.create(name="Net Admin One", system_prompt="x", owner_id="u1")
    agents.create(name="Net Admin Two", system_prompt="x", owner_id="u1")
    found = agent_tool._resolve("net admin", "u1", "admin")
    assert "_ambiguous" in found and len(found["_ambiguous"]) == 2


def test_resolution_only_sees_agents_the_user_may_use():
    from suni import agents
    agents.create(name="Someone Elses", system_prompt="x", owner_id="owner-1")
    assert agent_tool._resolve("Someone Elses", "other-user", "standard") is None
    assert agent_tool._resolve("Someone Elses", "owner-1", "standard") is not None


def test_exact_slug_wins_over_a_loose_name_match():
    from suni import agents
    agents.create(name="Ops", system_prompt="x", owner_id="u1")
    agents.create(name="Ops Secondary", system_prompt="x", owner_id="u1")
    assert agent_tool._resolve("ops", "u1", "admin")["slug"] == "ops"


# ── the intersection rule, read off the implementation ───────────────────────
def test_sub_turn_narrows_against_the_caller_not_the_role():
    src = inspect.getsource(agent_tool.handler)
    assert "CURRENT_GRANTS.get(None)" in src, \
        "the sub-turn does not consult the calling turn's grants"
    assert "caller.get(\"allowed_tools\")" in src
    assert "set(c_tools)" in src, "tools are not intersected with the caller's"


def test_caller_blocks_are_carried_into_the_sub_turn():
    src = inspect.getsource(agent_tool.handler)
    assert 'set(caller.get("blocked_tools")' in src, \
        "the caller's blocks are dropped when delegating"


def test_mcp_reach_is_intersected_too():
    src = inspect.getsource(agent_tool.handler)
    assert "c_mcp" in src and "set(c_mcp)" in src


def test_depth_is_restored_even_when_the_sub_turn_raises():
    src = inspect.getsource(agent_tool.handler)
    assert "finally:" in src and "AGENT_DEPTH.reset" in src, \
        "a failed delegation would leave the depth counter raised for the turn"


def test_failure_is_reported_not_papered_over():
    src = inspect.getsource(agent_tool.handler)
    assert "failed:" in src, "a failing agent is silently replaced by SUNI's own answer"


def test_unbound_orchestrator_says_so(monkeypatch):
    monkeypatch.setattr(agent_tool, "_orchestrator", None)
    out = _run(agent_tool.handler(agent="x", task="y"))
    assert "unavailable" in out
