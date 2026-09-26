"""A run that starts from a bare instruction should know why it exists.

A delegated agent is handed "summarise the Q3 numbers" in a fresh context with
no idea a board pack is being assembled, and a job that fires every Monday
cannot tell a first attempt from a fourth — including whether the last three
failed.

The note is background, never instruction: it says so, and it says not to repeat
itself, because a reply that narrates SUNI's plumbing is a bug this codebase has
already shipped once.
"""
import asyncio
import inspect

import pytest

from suni.core import ancestry
from suni.core.ancestry import delegation_note, schedule_note


# ── delegation ───────────────────────────────────────────────────────────────
def test_the_note_carries_the_persons_own_request():
    note = delegation_note("Preciso do pack para a reunião de quinta", "Researcher")
    assert "reunião de quinta" in note
    assert "Researcher" in note


def test_no_originating_request_means_no_note():
    # A note that says nothing costs tokens and invites the model to invent a
    # reason for the work.
    assert delegation_note("") == ""
    assert delegation_note("   ") == ""


def test_the_note_says_not_to_repeat_it():
    note = delegation_note("do the thing", "Helper")
    assert "not quote or mention" in note.lower()
    assert note.startswith("[") and note.endswith("]")


def test_a_long_request_is_trimmed_not_pasted_whole():
    note = delegation_note("x" * 5_000)
    assert len(note) < 600 and "…" in note


# ── schedules ────────────────────────────────────────────────────────────────
def test_a_first_run_is_told_it_is_the_first():
    note = schedule_note("Resumo semanal", "every monday at 08:00")
    assert "first run" in note
    assert "Resumo semanal" in note and "monday" in note


def test_a_failed_previous_run_is_reported_as_such():
    note = schedule_note("Resumo", "daily", "error: SMTP refused", "2026-09-25")
    assert "did not succeed" in note
    assert "SMTP refused" in note and "2026-09-25" in note


def test_a_successful_previous_run_is_not_described_as_a_failure():
    note = schedule_note("Resumo", "daily", "ok", "2026-09-25")
    assert "did not succeed" not in note and "finished" in note


def test_an_unattended_run_is_told_nobody_can_answer_questions():
    assert "nobody is watching" in schedule_note("J", "daily").lower()


# ── wiring: the notes have to reach the runs ─────────────────────────────────
def test_the_orchestrator_publishes_the_current_request():
    src = inspect.getsource(__import__("suni.core.orchestrator", fromlist=["x"]))
    assert "_REQ.set(user_input)" in src, "nothing ever sets the originating request"
    assert "_REQ.reset(_req_token)" in src, "a leaked request would cross turns"


def test_delegation_adds_the_note_to_the_sub_context():
    from suni.tools import agent_tool
    src = inspect.getsource(agent_tool)
    i = src.index("sub = _Ctx()")
    block = src[i:i + 700]
    assert "delegation_note" in block and "agent=\"ancestry\"" in block
    assert block.index("delegation_note") < block.index("_safe_run"), \
        "the note must be in the context before the run starts"


def test_the_scheduler_adds_the_note_before_running():
    from suni.web import server
    src = inspect.getsource(server)
    i = src.index("schedule_note as _sched_note")
    block = src[i:i + 700]
    assert "agent=\"ancestry\"" in block
    assert block.index("ctx.add") < block.index("_safe_run"), \
        "the note must be in the context before the run starts"


def test_the_request_is_task_local():
    """Two delegations at once must not see each other's origin."""
    async def one(value, hold):
        tok = ancestry.CURRENT_REQUEST.set(value)
        try:
            await asyncio.sleep(hold)
            return ancestry.CURRENT_REQUEST.get("")
        finally:
            ancestry.CURRENT_REQUEST.reset(tok)

    async def both():
        return await asyncio.gather(one("alpha", 0.02), one("beta", 0.01))

    assert asyncio.run(both()) == ["alpha", "beta"]
