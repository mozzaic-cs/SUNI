"""Asking several specialists at once, then answering as the head.

`invoke_agent` sends one task to one agent and waits. This is the other shape: a
head of operations asks the researcher and the bookkeeper in parallel and writes
the reply itself.

The parts worth pinning down are the failure paths. Parallelism multiplies cost,
so a spend ceiling has to stop the fan BEFORE the next specialist starts; and an
answer composed from two of five specialists must not read like an answer from
five, which means skipped and failed agents have to reach the composer by name.
"""
import asyncio

import pytest

from suni import usage
from suni.core import fanout


class FakeOrch:
    """Records what each specialist was asked, and can fail or bill on demand."""

    def __init__(self, answers=None, fails=(), cost=0):
        self.answers = answers or {}
        self.fails = set(fails)
        self.cost = cost
        self.asked: list[str] = []
        self.live = 0
        self.peak = 0

    async def _safe_run(self, task, ctx, agent_profile=None, **kw):
        slug = agent_profile["slug"]
        self.asked.append(slug)
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            await asyncio.sleep(0.02)
            if self.cost:
                usage.record(self.cost, 0)
            if slug in self.fails:
                raise RuntimeError("backend exploded")
            return self.answers.get(slug, f"{slug} says hello")
        finally:
            self.live -= 1


def _profiles(*slugs):
    return [{"slug": s, "name": s.title()} for s in slugs]


def _run(orch, profiles, **kw):
    acc, tok = usage.start()
    try:
        return asyncio.run(fanout.run_fanout(orch, "the request", profiles, **kw))
    finally:
        usage.reset(tok)


# ── the happy path ───────────────────────────────────────────────────────────
def test_every_specialist_is_asked_and_reported():
    orch = FakeOrch()
    out = _run(orch, _profiles("researcher", "bookkeeper"))
    assert sorted(orch.asked) == ["bookkeeper", "researcher"]
    assert [r["name"] for r in out] == ["Researcher", "Bookkeeper"], "order is preserved"
    assert all(r["answer"] for r in out)


def test_they_run_in_parallel_but_not_all_at_once():
    orch = FakeOrch()
    _run(orch, _profiles("a", "b", "c", "d"), max_parallel=2)
    assert orch.peak == 2, f"concurrency ceiling ignored (peak {orch.peak})"


def test_the_fan_is_capped_however_many_are_passed():
    orch = FakeOrch()
    out = _run(orch, _profiles(*[f"a{i}" for i in range(20)]))
    assert len(out) == fanout.MAX_AGENTS


# ── failure and spend ────────────────────────────────────────────────────────
def test_one_failure_does_not_lose_the_others():
    orch = FakeOrch(fails=["bookkeeper"])
    out = {r["slug"]: r for r in _run(orch, _profiles("researcher", "bookkeeper"))}
    assert out["researcher"]["answer"]
    assert "exploded" in out["bookkeeper"]["error"]
    assert "answer" not in out["bookkeeper"]


def test_the_budget_stops_the_fan_before_the_next_specialist_runs():
    # Each answer bills 1000; a 1500 budget must stop after the first.
    orch = FakeOrch(cost=1000)
    out = _run(orch, _profiles("a", "b", "c"), max_parallel=1, token_budget=1500)
    assert len(orch.asked) == 2, f"ran {orch.asked} — the ceiling did not bite"
    assert out[2]["skipped"], "an agent that never ran must say so"
    assert "budget" in out[2]["skipped"]


def test_no_budget_means_no_ceiling():
    orch = FakeOrch(cost=10_000)
    out = _run(orch, _profiles("a", "b", "c"), max_parallel=1, token_budget=0)
    assert all(r.get("answer") for r in out)


def test_an_empty_team_is_not_an_error():
    assert _run(FakeOrch(), []) == []


# ── what the head is told ────────────────────────────────────────────────────
def test_the_composer_is_given_every_answer():
    prompt = fanout.compose_prompt("q", [
        {"name": "Researcher", "answer": "the rate is 4%"},
        {"name": "Bookkeeper", "answer": "the invoice is unpaid"},
    ])
    assert "the rate is 4%" in prompt and "the invoice is unpaid" in prompt
    assert "q" in prompt


def test_a_missing_specialist_is_named_not_hidden():
    prompt = fanout.compose_prompt("q", [
        {"name": "Researcher", "answer": "found it"},
        {"name": "Bookkeeper", "skipped": "the turn's token budget was spent"},
        {"name": "Clerk", "error": "timed out"},
    ])
    assert "Bookkeeper" in prompt and "did not run" in prompt
    assert "Clerk" in prompt and "timed out" in prompt
    # And the head is told not to pass a partial answer off as a whole one.
    assert "do not present a partial answer" in prompt.lower()


def test_the_head_is_told_not_to_narrate_the_delegation():
    prompt = fanout.compose_prompt("q", [{"name": "A", "answer": "x"}])
    assert "do not describe this process" in prompt.lower()


def test_disagreement_is_surfaced_rather_than_averaged():
    assert "disagree" in fanout.compose_prompt("q", [{"name": "A", "answer": "x"}]).lower()


# ── the specialists still run the real path ──────────────────────────────────
def test_each_specialist_runs_through_the_ordinary_request_path():
    """Not a raw model call: that is how grants, model pins and daily ceilings
    keep applying to a delegated run."""
    import inspect
    src = inspect.getsource(fanout.run_fanout)
    assert "_safe_run" in src and "agent_profile=profile" in src


def test_the_specialist_is_told_why_it_was_asked():
    import inspect
    src = inspect.getsource(fanout.run_fanout)
    assert "delegation_note" in src, "a specialist with no ancestry answers a fragment"
