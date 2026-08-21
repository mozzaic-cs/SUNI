"""
"What would this agent do?" — answered without doing any of it.

The last of the three controls that make unattended agents defensible. Budgets
bound what an agent can spend and the report says what it did; this is the one
you use BEFORE letting it loose, and it is the only one of the three that has to
be careful about what it leaves behind.

Task mode does something similar — present a plan, wait for approval — and the
difference matters: a dry run must not stash that plan. An executable plan left
in memory turns a preview into something one word could fire.
"""
from __future__ import annotations

import inspect

from suni.core import orchestrator as orch


def _preview(src: str) -> str:
    i = src.index("if dry_run and iteration == 0:")
    return src[i:src.index('if conv_mode == "task" and iteration == 0:', i)]


SRC = inspect.getsource(orch)


def test_dry_run_is_threaded_all_the_way_down():
    """A flag that stops at the first function is the classic dead setting."""
    for fn in ("run", "_run_inner", "_safe_run", "_agent_loop"):
        i = SRC.index(f"async def {fn}(")
        sig = SRC[i:SRC.index(") ->", i)]
        assert "dry_run" in sig, f"{fn}() does not accept dry_run"
    assert "dry_run=dry_run" in SRC, "it is never passed on"


def test_nothing_is_executed():
    """The preview returns before the execution block."""
    body = _preview(SRC)
    assert "return Message(" in body
    assert "_execute_tool_calls" not in body


def test_the_plan_is_not_left_behind_to_be_approved():
    """Task mode stashes tool calls in _pending_plans for a later 'approve'.
    A preview that did the same would be a loaded gun."""
    body = _preview(SRC)
    assert "_pending_plans" not in body, \
        "the dry run stashes an executable plan"


def test_it_is_checked_before_task_mode():
    """Both trigger on iteration 0. Previewing must win over arming a plan."""
    dry = SRC.index("if dry_run and iteration == 0:")
    task = SRC.index('if conv_mode == "task" and iteration == 0:')
    assert dry < task


def test_the_preview_reports_the_grants_not_just_the_plan():
    """The governance question is what it COULD reach, not only what it chose
    this time — a different task might choose differently."""
    body = _preview(SRC)
    assert "Permitted at this moment:" in body
    for field in ("allowed_tools", "mcp_prefixes", "blocked_tools"):
        assert field in body, f"the preview omits {field}"


def test_an_agent_that_would_call_nothing_still_says_something():
    """Silence would read as 'it does nothing', which is not the same as
    'it answers without tools'."""
    body = _preview(SRC)
    assert "calling no tools" in body
    assert "response.content" in body


def test_the_output_is_unmistakable():
    body = _preview(SRC)
    assert "[DRY RUN — nothing was executed]" in body


def test_the_endpoint_resolves_the_profile_server_side():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    srv = (root / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    i = srv.index('@app.post("/api/agents/{slug}/dry-run")')
    block = srv[i:i + 1500]
    assert "list_for_user" in block, "no visibility check — any slug could be previewed"
    assert "dry_run=True" in block
    assert "agent.dry_run" in block, "previews are not audited"


def test_the_preview_runs_before_the_no_tool_calls_early_return():
    """The bug this caught: an agent that answers directly hit
    `if not response.has_tool_calls(): return response` first, so the dry run
    returned the raw answer with no marker at all — indistinguishable from a
    real reply, which is the one thing a preview must never be."""
    dry = SRC.index("if dry_run and iteration == 0:")
    early = SRC.index("if not response.has_tool_calls():\n                if response.content:")
    assert dry < early, "a tool-less dry run returns an unmarked answer"
