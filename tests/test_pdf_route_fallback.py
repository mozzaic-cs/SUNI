"""
A fast path whose precondition fails must hand back, not refuse.

Reported from live use, in Portuguese: "create a PDF with a short text about
Coimbra and email it to me". SUNI replied that it could not find any compiled
content and asked the user to compile it first — having done no research and
called no tools at all. The trace shows why:

    [ROUTE]   pdf
    [DONE]    total=3.75s route=pdf tools=[]

The direct-pdf path is an optimisation for "put THIS in a PDF": it takes the
last substantial assistant message as the document body. Asked to create a PDF
*about* something, with no prior turn to draw on, its precondition does not
hold — and it dead-ended instead of letting the general path research the topic
and call the tools.

The guard and the handler now share one definition of "is there content", so
the dispatch cannot admit a request the handler will refuse.
"""
from __future__ import annotations

import pathlib

from suni.core.orchestrator import _compiled_content
from suni.core.context import Context
from suni.core.context import Message, Role

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "suni" / "core" / "orchestrator.py").read_text(encoding="utf-8")


def _ctx(*messages):
    c = Context()
    for role, text in messages:
        c.add(Message(role=role, content=text, agent="t"))
    return c


def test_no_history_means_no_compiled_content():
    assert _compiled_content(_ctx()) == ""


def test_the_users_own_request_is_not_compiled_content():
    """Only assistant output counts — otherwise a long question would be
    mistaken for the document body."""
    long_ask = "Cria um pdf sobre Coimbra. " * 12
    assert _compiled_content(_ctx((Role.USER, long_ask))) == ""


def test_a_short_assistant_reply_does_not_count():
    assert _compiled_content(_ctx((Role.ASSISTANT, "Sure."))) == ""


def test_a_substantial_assistant_reply_counts():
    body = "Coimbra is a Portuguese city on the Mondego. " * 5
    assert _compiled_content(_ctx((Role.ASSISTANT, body))) == body


def test_the_most_recent_substantial_reply_wins():
    old = "First compilation about something else. " * 5
    new = "Second compilation, the one being referred to. " * 5
    got = _compiled_content(_ctx((Role.ASSISTANT, old), (Role.ASSISTANT, new)))
    assert got == new


def test_the_dispatch_guard_requires_content():
    """Without this the handler is reached and refuses — which is the reported
    bug."""
    i = SRC.index('elif route == "pdf"')
    guard = SRC[i:i + 500]
    assert "_compiled_content(context)" in guard, \
        "the pdf fast path can still be entered with nothing to compile"


def test_guard_and_handler_share_one_definition():
    """Two copies drifted apart once already: the guard admitted requests the
    handler then refused."""
    assert SRC.count("def _compiled_content") == 1
    assert SRC.count("_compiled_content(context)") >= 2


def test_the_refusal_is_translated_if_it_is_ever_reached():
    i = SRC.index("def _handle_pdf_direct")
    body = SRC[i:i + 2000]
    assert '_say("no_compiled_content")' in body
