"""
An agent profile that names a model must get that model.

The trap this guards: the tier system picks the agent for a request and escalates
to a stronger tier when the response looks weak. A pinned model that stayed
subject to escalation would be swapped out mid-run — the setting would appear to
work in casual use and silently stop applying on exactly the harder prompts the
model was chosen for. That is the failure shape this repository keeps producing,
so pinning suppresses escalation for the request.
"""
from __future__ import annotations

import inspect

import pytest

from suni.core import orchestrator as orch


@pytest.fixture(scope="module")
def src() -> str:
    return inspect.getsource(orch)


def test_pinned_model_is_taken_from_the_resolved_grants(src):
    """Not from the raw profile — grants are what passed through RBAC."""
    i = src.index("_pin_model")
    assert 'grants or {}).get("model")' in src[i:i + 200], \
        "the pinned model is read from somewhere other than the resolved grants"


def test_pinning_suppresses_escalation(src):
    """The whole point: a chosen model must still be answering at the end."""
    i = src.index("needs_escalation(response.content)")
    guard = src[i - 200:i + 60]
    assert "_pinned" in guard, \
        "escalation can still swap out a pinned model mid-request"


def test_escalation_still_works_when_not_pinned(src):
    """The guard must be a condition, not a removal."""
    assert "not _pinned and not response.has_tool_calls()" in src, \
        "escalation appears unconditionally disabled"
    assert "escalating %d→%d due to capability signal" in src, \
        "the escalation path was removed rather than guarded"


def test_construction_failure_falls_back_rather_than_failing(src):
    """A typo'd model name must not take the request down."""
    i = src.index("could not build agent for pinned model")
    assert "falling back to the tier system" in src[i:i + 200]
    fn = src[src.index("def _agent_for_model"):src.index("def _agent_for_tier")]
    assert "except Exception" in fn and "return None" in fn


def test_backend_is_cached_per_model():
    """Built once per model, not per request — construction opens a client."""
    fn = inspect.getsource(orch.Orchestrator._agent_for_model)
    assert "_model_agents" in fn and "if model in cache" in fn


def test_no_model_means_no_pin():
    """An agent profile without a model leaves tiering completely alone."""
    fn = inspect.getsource(orch.Orchestrator._agent_for_model)
    assert "if not model:" in fn and "return None" in fn


def test_pinned_agent_still_gets_the_base_system_prompt():
    """The base prompt carries the AI-disclosure instruction and safety rules;
    a pinned backend built without it would quietly drop both."""
    fn = inspect.getsource(orch.Orchestrator._agent_for_model)
    assert "resolve_system_prompt" in fn, \
        "the pinned backend is constructed without the base system prompt"


def test_agent_for_model_returns_none_on_failure(monkeypatch):
    """Functional check of the fallback, not just its source."""
    o = orch.Orchestrator.__new__(orch.Orchestrator)
    assert o._agent_for_model("") is None
    import suni.models.factory as factory
    monkeypatch.setattr(factory, "make_agent",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("no such model")))
    assert o._agent_for_model("does-not-exist:1b") is None
