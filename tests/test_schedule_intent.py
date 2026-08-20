"""
Scheduling understood without a model, and missing details asked for.

Registering create_schedule was not enough: across four live runs of the same
prompt the core tier called skills_list, run_shell, db_query and send_email —
never the right tool — with the tool registered, a hint injected and the agent
named in that hint. So the structured part is parsed deterministically here, and
these tests are the contract for that.

The other half is asking. A schedule built on a guessed email address sends
whatever it produces to a stranger, every day, with nobody watching.
"""
from __future__ import annotations

import inspect

import pytest

from suni.core import schedule_intent as si

AGENTS = [{"slug": "network-admin-agent", "name": "Network Admin Agent"},
          {"slug": "research-assistant", "name": "Research Assistant"}]


# ── the two prompts this was built for ───────────────────────────────────────
def test_daily_digest_prompt_is_understood_and_asks_for_the_address():
    p = si.parse("Suni please make a compilation of every task and scheduled entry "
                 "in my calendar every day at 8:00 AM delivered to my email.", AGENTS)
    assert p["cadence"] == "daily at 08:00"
    assert p["wants_email"] and not p["email_to"]
    assert si.question(p) and "email address" in si.question(p)


def test_hourly_agent_prompt_is_understood():
    p = si.parse('Suni please ask the "Network Admin Agent" to check if internal '
                 'company network services are running and healthy, every hour.', AGENTS)
    assert p["cadence"] == "hourly"
    assert p["agent_slug"] == "network-admin-agent"
    assert si.question(p) is None      # nothing missing: no delivery was asked for


# ── cadence parsing ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,cadence", [
    ("run a check every 15 minutes", "every 15m"),
    ("every 2 hours please", "every 2h"),
    ("hourly", "hourly"),
    ("summarise this daily at 07:30", "daily at 07:30"),
    ("every monday at 9:15 send a report", "weekly on mon at 09:15"),
    ("every day at 6pm", "daily at 18:00"),
    ("every day at 12am", "daily at 00:00"),
])
def test_cadences(text, cadence):
    assert si.parse(text, AGENTS)["cadence"] == cadence


def test_an_interval_is_not_read_as_a_time_of_day():
    """'every 2 hours' must not become 02:00 — the class of silent misreading
    this module exists to prevent."""
    assert si.parse("do it every 2 hours", AGENTS)["cadence"] == "every 2h"


def test_non_recurring_text_is_left_alone():
    for s in ("what is the weather", "write me a poem", "send this to bob@example.com"):
        assert si.parse(s, AGENTS)["recurring"] is False


def test_recurring_but_timeless_asks_rather_than_assuming():
    p = si.parse("send me a weekly report", AGENTS)
    assert p["recurring"] and p["cadence"] is None
    assert "what time of day" in si.question(p)


# ── never invent ─────────────────────────────────────────────────────────────
def test_an_address_is_never_invented():
    p = si.parse("email me a summary every day at 08:00", AGENTS)
    assert p["email_to"] == ""
    assert "which email address" in si.question(p)


def test_a_supplied_address_is_used():
    p = si.parse("email me a summary every day at 08:00 to ops@example.com", AGENTS)
    assert p["email_to"] == "ops@example.com"
    assert si.question(p) is None


def test_an_unknown_agent_name_is_reported_not_resolved():
    p = si.parse('ask the "Payroll Agent" to do it every hour', AGENTS)
    assert p["agent_named"] and p["agent_slug"] == ""


def test_agents_are_only_matched_against_the_ones_supplied():
    p = si.parse('ask the "Network Admin Agent" to do it hourly', [])
    assert p["agent_slug"] == ""


# ── the stored prompt ────────────────────────────────────────────────────────
def test_scheduling_clauses_are_stripped_from_the_stored_prompt():
    """The runner performs delivery. Leaving 'email it to me' in the prompt
    would make the scheduled run arrange a second one."""
    task = si.strip_scheduling("Suni please make a compilation of my calendar "
                               "every day at 8:00 AM delivered to my email.")
    low = task.lower()
    assert "every day" not in low and "8:00" not in low and "email" not in low
    assert "compilation of my calendar" in low


def test_stripping_never_returns_nothing():
    assert si.strip_scheduling("every day at 08:00").strip() != ""


def test_suggested_name_is_short_and_safe():
    n = si.suggest_name("make a compilation of every task and scheduled entry in my calendar")
    assert 0 < len(n) <= 60 and n[0].isupper()


# ── wiring ───────────────────────────────────────────────────────────────────
def test_the_direct_path_asks_before_creating_anything():
    from suni.core import orchestrator as orch
    src = inspect.getsource(orch.Orchestrator._handle_schedule_direct)
    q_at = src.index("_si.question(parsed)")
    create_at = src.index("_s.create(")
    assert q_at < create_at, "the schedule is created before the user is asked"


def test_the_direct_path_still_goes_through_approval():
    from suni.core import orchestrator as orch
    src = inspect.getsource(orch.Orchestrator._handle_schedule_direct)
    assert "request_approval" in src, "the direct path bypasses the approval gate"
    assert 'decision != "allow"' in src


def test_escalates_automatically_when_no_local_model_is_capable():
    from suni.core import orchestrator as orch
    import inspect as _i
    src = _i.getsource(orch)
    assert "_MIN_TIER_FOR_STRUCTURED" in src
    assert "no_capable_local" in src, "there is no automatic escalation path"
