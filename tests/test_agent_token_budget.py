"""An agent's daily ceiling counts tokens, not just runs.

A run count is a poor proxy for spend: one turn that delegates to the CLI and
reads forty documents costs orders of magnitude more than a one-line reply, and
both count as one run. The ceiling that matters is tokens.

The load-bearing test here is the last one. Token budgets read the audit trail,
and until Claude Code's own usage was booked there, the path that spends the
most recorded zero — so an agent with a token ceiling could run all day without
ever reaching it.
"""
from datetime import datetime, timezone

import pytest

from suni import agents, audit


@pytest.fixture(autouse=True)
def _isolated_audit(tmp_path, monkeypatch):
    """Never write to the real trail. agents.py reads it through audit.db_path(),
    so pointing _DB here moves the budget counters with it — which is the point
    of that accessor existing."""
    monkeypatch.setattr(audit, "_DB", tmp_path / "audit.db")
    audit.init_db()
    yield


def _spend(slug, prompt=0, gen=0, route="chat"):
    audit.log("u1", "someone", route=route, prompt_tokens=prompt,
              gen_tokens=gen, agent_slug=slug)


def test_no_ceiling_means_no_refusal():
    assert agents.budget_exceeded({"slug": "a", "max_runs_day": 0,
                                   "max_tokens_day": 0}) == ""


def test_a_token_ceiling_stops_the_agent_when_it_is_reached():
    prof = {"slug": "researcher", "max_tokens_day": 10_000}
    _spend("researcher", prompt=4_000, gen=500)
    assert agents.budget_exceeded(prof) == "", "under the ceiling, it runs"
    _spend("researcher", prompt=5_000, gen=600)
    hit = agents.budget_exceeded(prof)
    assert hit.startswith("tokens:"), hit
    # The reason carries the numbers so the refusal can say what to raise.
    _, limit, spent = hit.split(":")
    assert int(limit) == 10_000
    assert int(spent) == 10_100


def test_the_run_ceiling_still_works_and_is_reported_separately():
    prof = {"slug": "poller", "max_runs_day": 2}
    _spend("poller", prompt=1, gen=1)
    _spend("poller", prompt=1, gen=1)
    assert agents.budget_exceeded(prof) == "runs:2"


def test_one_agents_spending_is_not_charged_to_another():
    _spend("noisy", prompt=50_000, gen=1_000)
    assert agents.budget_exceeded({"slug": "quiet", "max_tokens_day": 100}) == ""


def test_tokens_today_counts_every_route_not_only_chat():
    # A scheduled run is exactly the unattended case a budget exists for, and it
    # is not route='chat'. Counting only chat would leave schedules unbounded.
    _spend("nightly", prompt=900, gen=100, route="schedule")
    assert agents.tokens_today("nightly") == 1_000


def test_claude_code_usage_is_booked_so_the_ceiling_can_bind():
    from suni.models.claude_code_agent import _record_cc_usage
    from suni import usage as _usage

    acc, tok = _usage.start()
    try:
        # The shape the CLI prints on its result line. Cache reads and writes are
        # input tokens, and on a resumed session they are most of the bill.
        _record_cc_usage({"usage": {"input_tokens": 12,
                                    "cache_creation_input_tokens": 27_312,
                                    "cache_read_input_tokens": 4_000,
                                    "output_tokens": 418}})
        assert acc.prompt == 31_324, "cache tokens must be counted"
        assert acc.gen == 418
    finally:
        _usage.reset(tok)


def test_a_result_line_without_usage_changes_nothing():
    from suni.models.claude_code_agent import _record_cc_usage
    from suni import usage as _usage
    acc, tok = _usage.start()
    try:
        _record_cc_usage({"result": "hello"})
        assert acc.prompt == 0 and acc.gen == 0
    finally:
        _usage.reset(tok)
