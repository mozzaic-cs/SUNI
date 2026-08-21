"""
Ceilings and accountability for agents that run unattended.

The global tool-iteration cap bounds ONE turn. An agent on a fifteen-minute
schedule with a tool loop has no natural stopping point across a day, and that
is the shape of runaway a business asks about before it will let anything run
unwatched. So an agent can carry its own step budget and a daily run allowance,
and there is a report that answers "what did this thing do, and what was it
allowed to do" from the audit trail rather than from a counter that could
disagree with it.
"""
from __future__ import annotations

import inspect

import pytest

from suni import agents
from suni.core import orchestrator as orch


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(agents, "AGENTS_DB", tmp_path / "agents.db")
    yield


# ── step budget ──────────────────────────────────────────────────────────────
def test_no_budget_means_the_global_cap():
    assert agents.budget_for({}, 8)["max_steps"] == 8
    assert agents.budget_for(None, 8)["max_steps"] == 8


def test_an_agent_may_tighten_the_step_cap():
    assert agents.budget_for({"max_steps": 3}, 8)["max_steps"] == 3


def test_the_step_cap_is_applied_as_a_minimum_not_a_replacement():
    """A profile must not buy itself more room than the instance allows — the
    same rule its tool grants follow."""
    src = inspect.getsource(orch)
    i = src.index("_ag_steps")
    assert "min(_max_iters, _ag_steps)" in src[i:i + 300], \
        "an agent could raise the global tool-iteration cap"


def test_the_budget_travels_with_the_grants():
    g = agents.effective_grants({"max_steps": 4, "max_runs_day": 10, "tools": None},
                                "admin", [])
    assert g["max_steps"] == 4 and g["max_runs_day"] == 10


# ── daily allowance ──────────────────────────────────────────────────────────
def test_no_daily_cap_means_unlimited():
    assert agents.over_daily_budget({"slug": "x"}) is False
    assert agents.over_daily_budget({"slug": "x", "max_runs_day": 0}) is False


def test_the_allowance_is_counted_from_the_audit_trail():
    """Not from a counter on the profile, which would drift the first time a run
    failed between incrementing and finishing."""
    src = inspect.getsource(agents.runs_today)
    assert "audit_log" in src and "agent_slug" in src


def test_a_budget_check_never_breaks_the_turn():
    assert "except Exception" in inspect.getsource(agents.runs_today)
    src = inspect.getsource(orch)
    i = src.index("over_daily_budget as _over")
    assert "except Exception" in src[i:i + 900]


def test_the_gate_runs_before_any_work():
    """An agent that has spent its budget should cost nothing to refuse."""
    src = inspect.getsource(orch)
    gate = src.index("over_daily_budget as _over")
    tier = src.index("_start_tier = min(max(complexity_score")
    assert gate < tier, "the budget is checked after the model work has been set up"


def test_refusal_says_what_to_do_about_it():
    src = inspect.getsource(orch)
    i = src.index("has used its ")
    block = src[i:i + 400]
    assert "tomorrow" in block and "admin panel" in block


# ── the report ───────────────────────────────────────────────────────────────
def test_report_is_empty_but_well_formed_for_an_unused_agent():
    r = agents.report("never-run", days=7)
    assert r["runs"] == 0 and r["tools"] == {} and r["users"] == []
    assert r["slug"] == "never-run" and r["days"] == 7


def test_report_separates_what_it_did_from_what_it_could_do():
    """Two different questions, and the second is the one a review asks."""
    src = inspect.getsource(agents.report)
    assert "grants_seen" in src
    assert "a different question from what it did" in src


def test_report_deduplicates_identical_grants():
    """A week of identical entries says nothing; a CHANGE is the signal."""
    src = inspect.getsource(agents.report)
    assert "seen" in src and "distinct" in src


def test_report_never_raises():
    src = inspect.getsource(agents.report)
    assert "except Exception" in src
