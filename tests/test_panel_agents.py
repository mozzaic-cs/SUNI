"""
Panel agents: an agent whose model is a panel rather than a model.

SUNI already had draft → cross-critique → synthesise across decorrelated models,
and it already had agent profiles. They were separate features. An agent that
convenes a panel is the combination, and it needs no new machinery: a profile
carries mode=collaborate, so invoking it lands in the existing branch.

The ordering test below matters more than it looks. Grants — and therefore the
profile's mode — used to be resolved AFTER the collaborate branch, so a panel
agent's mode reached nothing. That is this codebase's signature defect, occurring
inside the code that exists to stop a profile reaching too far.
"""
from __future__ import annotations

import inspect
import pathlib

import pytest

from suni.core import orchestrate as collab

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def orch_src() -> str:
    return (ROOT / "suni" / "core" / "orchestrator.py").read_text(encoding="utf-8")


# ── the ordering that made it work at all ────────────────────────────────────
def test_grants_resolve_before_the_collaborate_branch(orch_src):
    grants = orch_src.index("_agent_grants = _eff(")
    branch = orch_src.index('if conv_mode == "collaborate"')
    assert grants < branch, \
        "a panel agent's mode=collaborate is resolved after the branch that reads it"


def test_grants_resolve_before_the_tier_decision(orch_src):
    grants = orch_src.index("_agent_grants = _eff(")
    tier = orch_src.index("_start_tier = min(max(complexity_score")
    assert grants < tier, "tier decisions read grants that have not been resolved yet"


def test_the_mode_still_passes_through_the_role_clamp(orch_src):
    """A profile must not hand out collaborate to a role that may not use it —
    effective_grants() clamps, and conv_mode must come from the clamped value."""
    i = orch_src.index("conv_mode = _agent_grants")
    assert '_agent_grants["mode"]' in orch_src[i - 200:i + 120]


# ── the panel itself ─────────────────────────────────────────────────────────
def test_a_profile_may_bring_its_own_pool(orch_src):
    """A reviewer's panel is not a summariser's."""
    assert 'agent_profile or {}).get("pool")' in orch_src
    assert "pool=_pool" in orch_src


def test_the_persona_shapes_only_the_synthesis():
    """Applying the agent's voice to every seat would correlate the models, and
    decorrelation is the entire reason a panel earns its cost."""
    src = inspect.getsource(collab.run_collaboration)
    assert "persona" in src and "context_hint" in src
    assert "correlate" in src, "the reason is not recorded where it can be undone"


def test_pool_override_falls_back_to_config():
    src = inspect.getsource(collab._build_pool)
    assert "pool or _cfg.get" in src, "a per-agent pool replaces the global one silently"


def test_the_answer_is_attributed_to_the_agent(orch_src):
    """Otherwise a panel answer is indistinguishable from an ordinary one."""
    assert "· panel]" in orch_src


def test_no_profile_means_the_ordinary_collaborate_path(orch_src):
    """Selecting collaborate mode by hand must still work with no agent."""
    i = orch_src.index("_pool = (agent_profile or {})")
    block = orch_src[i:i + 400]
    assert "or {}" in block, "the branch assumes a profile is present"
    assert "or None" in block


def test_run_collaboration_still_works_without_the_new_arguments():
    """Existing callers pass neither pool nor persona."""
    sig = inspect.signature(collab.run_collaboration)
    assert sig.parameters["pool"].default is None
    assert sig.parameters["persona"].default == ""
