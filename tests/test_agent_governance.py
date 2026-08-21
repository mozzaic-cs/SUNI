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


# ── the budget must survive the way it is actually set ───────────────────────
def test_create_accepts_a_budget():
    """It did not, and the admin form posted the fields anyway — so a limit typed
    into the UI was silently dropped. The unreachable-control pattern, inside the
    feature added to make agents controllable."""
    rec = agents.create(name="Bounded", system_prompt="x", owner_id="u1",
                        max_steps=3, max_runs_day=10)
    assert rec["max_steps"] == 3 and rec["max_runs_day"] == 10
    got = agents.get("bounded")
    assert got["max_steps"] == 3 and got["max_runs_day"] == 10


def test_the_create_endpoint_forwards_the_budget():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    srv = (root / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    i = srv.index('@app.post("/api/agents")')
    block = srv[i:srv.index('@app.get("/api/agents/{slug}")', i)]
    assert "max_steps=" in block and "max_runs_day=" in block, \
        "the endpoint drops the budget the form sends"


def test_the_admin_form_and_the_endpoint_agree():
    """Both halves must use the same field names or the value vanishes between
    them — which is exactly how this was missed."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    html = (root / "suni" / "web" / "admin.html").read_text(encoding="utf-8")
    assert "max_steps: parseInt" in html and "max_runs_day: parseInt" in html


def test_budget_columns_are_added_to_an_existing_database(tmp_path, monkeypatch):
    """The upgrade path, which a fresh-database test cannot see.

    CREATE TABLE IF NOT EXISTS leaves an existing table alone, so a column added
    after release never appears on an upgraded install — create() failed with
    "table agents has no column named max_steps" against a real deployment while
    every test passed.
    """
    import sqlite3
    db = tmp_path / "old.db"
    # A pre-budget schema, as an older install would have.
    with sqlite3.connect(db) as c:
        c.execute("""CREATE TABLE agents (
            slug TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            owner_id TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'assistant', tools_json TEXT NOT NULL DEFAULT 'null',
            blocked_json TEXT NOT NULL DEFAULT '[]', mcp_json TEXT NOT NULL DEFAULT 'null',
            enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, used_count INTEGER NOT NULL DEFAULT 0,
            last_used TEXT NOT NULL DEFAULT '')""")
    monkeypatch.setattr(agents, "AGENTS_DB", db)
    monkeypatch.setattr(agents, "AGENTS_DIR", tmp_path / "agents")
    rec = agents.create(name="Upgraded", system_prompt="x", owner_id="u1", max_runs_day=5)
    assert rec["max_runs_day"] == 5
    assert agents.get("upgraded")["max_runs_day"] == 5
