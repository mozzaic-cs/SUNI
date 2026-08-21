"""
An agent that declares tools gets a model that can call them.

The failure this addresses was observed, not theorised: a delegated agent with
ping_host permitted replied with the literal text ping_host("localhost") instead
of invoking it. The profile was right, the tool was allowed, and the model wrote
a function call as prose.

Declaring tools is the user saying "this agent uses these", so it is treated as
a capability requirement. Requiring them to know which local model does
function-calling well would be pushing an implementation detail onto the person
least placed to judge it.
"""
from __future__ import annotations

import inspect
import re

import pytest

from suni.core import orchestrator as orch


@pytest.fixture(scope="module")
def src() -> str:
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    return (root / "suni" / "core" / "orchestrator.py").read_text(encoding="utf-8")


def _block(src: str) -> str:
    i = src.index("An agent that declares tools needs a model that can actually call")
    return src[i:src.index('_log.info("[TIER]    start=', i)]


def test_declaring_tools_raises_the_tier(src):
    b = _block(src)
    assert "_MIN_TIER_FOR_TOOL_USE" in b
    assert "_start_tier = _MIN_TIER_FOR_TOOL_USE" in b


def test_the_trigger_is_an_explicit_tool_declaration(src):
    """Not 'any agent' — an agent with no declared tools inherits the caller's
    set and carries no extra capability requirement."""
    b = _block(src)
    assert 'agent_profile.get("tools") is not None' in b


def test_a_pinned_model_is_never_overridden(src):
    """Silently replacing a deliberate choice is the same class of bug as
    escalation swapping a pinned model out mid-run."""
    b = _block(src)
    assert 'not (_agent_grants or {}).get("model")' in b


def test_escalates_when_no_local_tier_qualifies(src):
    b = _block(src)
    assert "CLAUDE_CODE_TIER" in b
    assert "agent_needs_tools" in b, "no reason is reported on the escalation event"


def test_says_so_when_it_cannot_escalate(src):
    """Nothing capable and no handoff: warn, rather than let it fail quietly as
    prose-instead-of-tool-call."""
    b = _block(src)
    assert "_log.warning" in b
    assert "may not be reliable" in b


def test_the_two_thresholds_are_separate_constants(src):
    """Structured tool SELECTION and well-formed tool CALLS are different
    problems that happen to share a number today."""
    assert "_MIN_TIER_FOR_STRUCTURED = 3" in src
    assert "_MIN_TIER_FOR_TOOL_USE = 3" in src


def test_routing_never_lowers_the_tier(src):
    """complexity_score may already have chosen higher; this is a floor."""
    b = _block(src)
    assert "if _start_tier < _MIN_TIER_FOR_TOOL_USE:" in b, \
        "the floor is applied unconditionally and could demote a harder task"


def test_no_agent_profile_leaves_tiering_untouched(src):
    b = _block(src)
    assert b.lstrip().startswith("# An agent") or "if agent_profile and" in b
    assert "if agent_profile and" in b
