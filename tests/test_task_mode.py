"""Task mode: the one mode whose entire purpose is not acting until it is told.

It writes out the tool calls it intends to make and waits. Two things were
wrong with the waiting.

THE ANSWER WAS READ BY SUBSTRING. `any(w in lower for w in ("approve", "yes",
"ok", "proceed", "go", "sim"))` — so "ok" matched inside "looks", "go" inside
"goal", "sim" inside "assim". A reply of "that looks dangerous, stop" APPROVED
the plan and ran it.

THE PLAN WAS KEYED BY MEMORY ADDRESS. `self._pending_plans[id(context)]`.
Sessions are evicted on a timer, the Context is freed, and CPython reuses the
address — measured reuse on the second allocation. A new conversation could
inherit the previous one's queued tool calls and run them on "ok". The
dictionary was never cleaned either, which is what left entries lying about to
be matched.
"""
from __future__ import annotations

import gc
import pathlib

import pytest

from suni.core.context import Context
from suni.core.orchestrator import _read_plan_reply

SRC = (pathlib.Path(__file__).resolve().parent.parent
       / "suni/core/orchestrator.py").read_text(encoding="utf-8")


# ── reading the answer ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "approve", "Approve.", "  OK  ", "ok", "yes", "y", "proceed", "go ahead",
    "sim", "aprovar", "podes avançar", "confirmo",
])
def test_an_approval_is_an_approval(text):
    assert _read_plan_reply(text) == "approve"


@pytest.mark.parametrize("text", [
    "cancel", "no", "stop", "abort", "nao", "não", "cancela", "esquece",
])
def test_a_refusal_is_a_refusal(text):
    assert _read_plan_reply(text) == "cancel"


@pytest.mark.parametrize("text", [
    "that looks dangerous, stop",        # "ok" inside "looks"
    "what is the goal here?",            # "go" inside "goal"
    "assim nao",                         # "sim" inside "assim" — and it means NO
    "faz isso de forma simples",         # "sim" inside "simples"
    "I googled it and it seems wrong",   # "go" inside "googled"
    "take a look at it first",           # "ok" inside "look"
    "não sei, o que achas?",
])
def test_a_sentence_is_never_an_approval(text):
    """Every one of these approved a plan of tool calls before. The failure is
    the direction that matters: a plan dropped costs a sentence, a plan run by
    accident costs whatever it was about to do."""
    assert _read_plan_reply(text) != "approve", f"{text!r} still approves"


def test_an_empty_reply_is_not_an_approval():
    for text in ("", "   ", None, "..."):
        assert _read_plan_reply(text) != "approve"


# ── where the plan lives ─────────────────────────────────────────────────────

def test_the_plan_is_not_keyed_by_a_memory_address():
    assert "_pending_plans" not in SRC, (
        "the plan is still held in a dictionary keyed by id(context)"
    )
    assert 'context.set("pending_plan"' in SRC
    assert 'context.get("pending_plan")' in SRC


def test_a_plan_belongs_to_one_conversation_and_dies_with_it():
    """The hazard, demonstrated: an address freed by one Context is handed
    straight back to the next. Holding the plan ON the context means there is
    no shared table for a new conversation to match against."""
    # Free-then-allocate, which is what _get_context does on the request after
    # a session is evicted. That pattern reproduces reuse within a couple of
    # cycles; allocating fifty at once does not, because they all coexist.
    reused = None
    for _ in range(80):
        a = Context()
        a.set("pending_plan", ["rm -rf something"])
        addr = id(a)
        del a
        gc.collect()
        b = Context()
        if id(b) == addr:
            reused = b
            break
        del b
    if reused is None:
        pytest.skip("the address was not reused in this run; the point stands")
    assert reused.get("pending_plan") is None, (
        "a new conversation inherited the previous one's queued tool calls"
    )


def test_anything_that_is_not_an_approval_drops_the_plan():
    """Read from the branch itself: the else arm must clear it, or a plan
    survives a refusal and waits for a later "ok"."""
    i = SRC.index('if conv_mode == "task" and context.get("pending_plan")')
    branch = SRC[i:i + 2200]
    assert 'context.set("pending_plan", None)' in branch
    # ...on both arms: after running it, and after refusing it.
    assert branch.count('context.set("pending_plan", None)') >= 2, (
        "the plan is cleared on one path only"
    )
