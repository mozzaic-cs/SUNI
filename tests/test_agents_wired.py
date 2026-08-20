"""
An agent profile's settings must REACH the orchestrator.

tests/test_agents.py proves effective_grants() computes the right answer. That
is not the same as proving the answer is used — this repository's recurring
defect is a setting that resolves correctly and then reaches nothing, which is
why tests/test_settings_are_wired.py exists.

So these assert against the source of the call path rather than against a live
model: that the profile is threaded run() -> _run_inner -> _agent_loop, that the
grants are what get handed to the tool registry, and that the plain-role
behaviour is untouched when no profile is given.
"""
from __future__ import annotations

import inspect
import re

import pytest

from suni.core import orchestrator as orch


@pytest.fixture(scope="module")
def src() -> str:
    return inspect.getsource(orch)


def _sig(src: str, name: str) -> str:
    i = src.index(f"async def {name}(")
    return src[i:src.index(") ->", i)]


def test_profile_is_accepted_and_threaded(src):
    """A parameter nobody passes on is the classic dead setting."""
    assert "agent_profile" in _sig(src, "run"), "run() does not accept a profile"
    assert "agent_profile" in _sig(src, "_run_inner"), "_run_inner does not accept it"
    call = src[src.index("await self._run_inner("):]
    assert "agent_profile" in call[:call.index(")")], \
        "run() accepts a profile but never passes it to _run_inner"


def test_grants_reach_the_tool_registry(src):
    """The registry call decides what the model can actually invoke."""
    i = src.index("self.registry.get_ollama_tools(")
    call = src[i:src.index(")", src.index("blocked_tools=", i))]
    assert "grants[" in call, \
        "get_ollama_tools() still reads RBAC directly — agent grants are ignored"
    assert "allowed_tools" in call and "blocked_tools" in call


def test_mcp_prefixes_come_from_grants(src):
    i = src.index("role_prefixes =")
    assert "grants[" in src[i - 400:i + 200], \
        "MCP prefixes are not taken from the resolved grants"


def test_grants_are_resolved_through_effective_grants(src):
    """Resolution must go through the intersecting function, not read the
    profile's declared fields directly — that would be the escalation."""
    assert "effective_grants" in src, "the profile is used without intersecting it"
    assert re.search(r"_eff\(\s*\n?\s*agent_profile,\s*user_role", src), \
        "effective_grants() is not called with the caller's role"


def test_no_profile_keeps_plain_role_behaviour(src):
    """The feature must be inert when unused."""
    i = src.index("self.registry.get_ollama_tools(")
    call = src[i:src.index(")", src.index("blocked_tools=", i))]
    assert "_rbac.allowed_tools(user_role)" in call, \
        "the plain-role path was removed; requests without a profile lose their grants"


def test_prompt_is_added_not_substituted(src):
    """The base prompt carries the AI-disclosure instruction (EU AI Act Art 50)
    and the safety rules. A profile must not be able to replace it."""
    i = src.index('agent="agent-profile"')
    block = src[i - 700:i + 100]
    assert "context.add(" in block, "the profile prompt is not injected into context"
    assert "resolve_system_prompt" not in block, \
        "the profile appears to replace the base system prompt rather than add to it"


def test_mode_cannot_be_widened_by_the_profile(src):
    """conv_mode may only be reassigned from the RESOLVED grants, which have
    already been clamped to the role's allowed modes."""
    i = src.index("conv_mode = _agent_grants")
    assert '_agent_grants["mode"]' in src[i - 200:i + 120], \
        "conv_mode is set from something other than the resolved grants"
