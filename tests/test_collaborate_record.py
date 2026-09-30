"""What the panel leaves behind, and what it admits to.

Asked, in collaborate mode, which models had produced an answer, SUNI said
she was a single model and had no record of how the reply was made.
Both halves of that were the code's doing rather than hers: the synthesis
prompt told her never to mention the other models, and the panel logged
nothing at all, so no record existed to consult.

These run the real collaborate() over stub seats. The phases are the point, so
they are exercised rather than grepped for.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib

import pytest

from suni.core import orchestrate as C
from suni.core.message import Message, Role

SRC = (pathlib.Path(__file__).resolve().parent.parent
       / "suni/core/orchestrate.py").read_text(encoding="utf-8")


class Seat:
    """A model that answers, or fails in one of the ways real seats fail."""

    def __init__(self, name, reply="", raises=False):
        self.name = name
        self.reply = reply
        self.raises = raises
        self.prompts = []

    async def chat(self, messages, context):
        self.prompts.append(messages[-1].content)
        if self.raises:
            raise RuntimeError("cli not found")
        return Message(role=Role.ASSISTANT, content=self.reply, agent=self.name)


def _run(seats, **kw):
    return asyncio.run(C.collaborate("What is the best model?", seats, **kw))


def test_the_panel_is_named_in_the_log(caplog):
    """No trace of the seats survived a turn, so the question 'which models
    answered this?' had no answer anywhere on the machine."""
    with caplog.at_level(logging.INFO, logger="suni.orchestrate"):
        _run([Seat("claude-code", "Draft A."), Seat("codex", "Draft B.")])
    text = caplog.text
    assert "[COLLAB] panel of 2" in text
    assert "claude-code" in text and "codex" in text
    assert "drafted:" in text
    assert "synthesised by" in text


def test_a_silent_seat_is_named_and_the_degrade_is_loud(caplog):
    """The failure that looks like success: the answer still arrives and reads
    perfectly well, having had no cross-check at all."""
    with caplog.at_level(logging.INFO, logger="suni.orchestrate"):
        out = _run([Seat("claude-code", "Only draft."), Seat("codex", raises=True)])
    assert out == "Only draft."
    assert "DEGRADED to a single model" in caplog.text
    assert "codex" in caplog.text
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "losing half the panel is not an INFO-level event"
    )


def test_a_seat_that_answers_with_its_own_error_is_not_a_draft(caplog):
    """A CLI that exits non-zero does not raise — the agent hands the failure
    back as its answer. Counted as a draft it gets peer-reviewed and folded
    into the synthesis, so a broken seat does not merely fail to help."""
    broken = Seat("codex", "Codex returned an error (exit 1): command not found")
    with caplog.at_level(logging.INFO, logger="suni.orchestrate"):
        out = _run([Seat("claude-code", "A real answer."), broken])
    assert "answered with a failure" in caplog.text
    assert "DEGRADED" in caplog.text, "the broken seat still counted as live"
    assert out == "A real answer."


def test_a_long_answer_about_errors_is_still_an_answer():
    """The guard above must not throw away a genuine reply that happens to
    discuss a command that was not found."""
    essay = ("When a shell reports command not found it usually means the "
             "binary is absent from PATH. " * 12)
    assert not C._looks_like_failure(essay)
    assert C._looks_like_failure("Codex returned an error (exit 1): no output")


def test_the_synthesiser_is_told_who_was_at_the_table():
    """It only ever sees the drafts, never who produced them — so when asked,
    it answered as the single model it is."""
    seats = [Seat("claude-code", "Draft A."), Seat("codex", "Draft B.")]
    _run(seats)
    synth = seats[0].prompts[-1]
    assert "panel of 2 models" in synth
    assert "claude-code" in synth and "codex" in synth


def test_it_may_tell_the_truth_when_it_is_asked_for_it():
    """Concealing the process in an ordinary answer is a presentation choice.
    Denying it to somebody who asks point blank is a different thing, and not
    one to make on a product that carries an AI-disclosure line."""
    seats = [Seat("claude-code", "Draft A."), Seat("codex", "Draft B.")]
    _run(seats)
    synth = seats[0].prompts[-1]
    assert "do NOT mention the other models" not in synth, (
        "the blanket gag is still in place"
    )
    assert "truthfully and plainly" in synth
    assert "Do not claim to be a single model" in synth
    # ...while still staying backstage for an ordinary answer.
    assert "do not narrate" in synth


def test_it_is_not_invited_to_invent_a_thought_process():
    """It can see the drafts and critiques; it cannot see any model's internal
    reasoning, and should not describe what it does not have."""
    seats = [Seat("claude-code", "Draft A."), Seat("codex", "Draft B.")]
    _run(seats)
    synth = seats[0].prompts[-1]
    assert "not any model's" in synth and "internal reasoning" in synth


def test_the_panel_still_works_when_nobody_answers(caplog):
    with caplog.at_level(logging.INFO, logger="suni.orchestrate"):
        out = _run([Seat("a", raises=True), Seat("b", raises=True)])
    assert "did not return an answer" in out
    assert "no model answered" in caplog.text
