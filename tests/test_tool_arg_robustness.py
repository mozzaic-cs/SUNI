"""
A hallucinated argument must not take down the tool call.

From live use. Asked, in Portuguese, to make a PDF about Coimbra and email it,
qwen2.5:7b folded the recipient into the first tool it reached:

    create_pdf(content=…, path=…, to="user@example.com")

`handler(**args)` raised `TypeError: unexpected keyword argument 'to'`, the raw
Python message was returned as the tool result, and the model answered the user
with a lecture about correct JSON formatting instead of retrying. Nothing was
created and nothing was sent.

Small models do this — they merge the whole request into whichever tool they
call first. The registry is the right place to absorb it: one filter protects
every tool, rather than each handler growing a `**kwargs` it never asked for.

Dropping silently would be its own bug, though: the user asked for an email, and
the email is the part being dropped. So the result carries a note naming what
was ignored, which is the model's cue to make the second call.
"""
from __future__ import annotations

import asyncio

import pytest

from suni.tools.registry import ToolRegistry

SCHEMA = {
    "name": "make_thing",
    "description": "d",
    "parameters": {"type": "object",
                   "properties": {"content": {"type": "string"},
                                  "path": {"type": "string"}},
                   "required": ["content"]},
}


def _reg(handler, schema=SCHEMA):
    r = ToolRegistry()
    r.register(schema, handler)
    return r


def _run(reg, name, args):
    return asyncio.run(reg.execute(name, args))


# ── the reported failure ─────────────────────────────────────────────────────
def test_an_unexpected_argument_no_longer_fails_the_call():
    def handler(content, path=""):
        return f"made {content} at {path}"
    out = str(_run(_reg(handler), "make_thing",
                   {"content": "x", "path": "p", "to": "a@b.c"}))
    assert "made x at p" in out
    assert "unexpected keyword" not in out, "the TypeError still reaches the model"


def test_the_model_is_told_what_was_ignored():
    """Silently dropping 'to' would lose the email the user asked for, with
    nothing to indicate it."""
    def handler(content):
        return "ok"
    out = str(_run(_reg(handler), "make_thing", {"content": "x", "to": "a@b.c"}))
    assert "ignored" in out and "to" in out
    assert "call that tool now" in out, "no cue to make the follow-up call"


def test_nothing_is_appended_when_every_argument_was_valid():
    """The note must not appear on ordinary calls, or it becomes noise the
    model learns to ignore."""
    def handler(content, path=""):
        return "ok"
    out = str(_run(_reg(handler), "make_thing", {"content": "x", "path": "p"}))
    assert "ignored" not in out


# ── what must NOT be stripped ────────────────────────────────────────────────
def test_declared_arguments_survive():
    seen = {}

    def handler(content, path=""):
        seen.update(content=content, path=path)
        return "ok"
    _run(_reg(handler), "make_thing", {"content": "c", "path": "p"})
    assert seen == {"content": "c", "path": "p"}


def test_runtime_injected_parameters_survive():
    """_user_id is filled by the runtime and is deliberately absent from the
    schema; intersecting with the schema alone would discard it."""
    seen = {}

    def handler(content, _user_id=""):
        seen["uid"] = _user_id
        return "ok"
    _run(_reg(handler), "make_thing", {"content": "c"})
    assert "uid" in seen, "the system parameter was stripped or never injected"


def test_a_handler_taking_kwargs_is_left_alone():
    """It opted into whatever arrives; filtering would change its behaviour."""
    seen = {}

    def handler(content, **kw):
        seen.update(kw)
        return "ok"
    out = str(_run(_reg(handler), "make_thing", {"content": "c", "to": "a@b.c"}))
    assert seen == {"to": "a@b.c"}
    assert "ignored" not in out


def test_a_missing_required_argument_still_errors():
    """Stripping unknowns must not turn a genuinely malformed call into a
    silent success."""
    def handler(content):
        return "ok"
    out = str(_run(_reg(handler), "make_thing", {"to": "a@b.c"}))
    assert "error" in out.lower()


def test_an_unknown_tool_still_reports_clearly():
    def handler(content):
        return "ok"
    out = str(_run(_reg(handler), "no_such_tool", {"content": "x"}))
    assert "not found" in out


# ── the real registry, with the real call that failed ────────────────────────
def test_the_reported_call_succeeds_against_the_real_registry(tmp_path):
    from suni.main import _build_registry
    reg = _build_registry()
    out = str(_run(reg, "create_pdf", {
        "content": "Coimbra é uma cidade portuguesa.",
        "path": str(tmp_path / "coimbra.pdf"),
        "to": "user@example.com"}))
    assert "PDF created" in out, out
    assert (tmp_path / "coimbra.pdf").exists()
    assert "ignored these arguments: to" in out
