"""
Recognising that a tool failed, and not asking to retry it forever.

From live use, 2026-08-26. A rejected SMTP password produced this, five times:

    [APPROVAL] requesting send_email(to=…, subject=Informação sobre …)
    [APPROVAL] resolved … → allow
    [TOOL] send_email  Failed to send email: (535, '5.7.8 authentication failed')
    [APPROVAL] requesting send_email(to=recipient@example.com, subject=Test Email)
    …

Two separate defects, one visible and one not:

1. The orchestrator decided a tool had failed with `str(result).startswith
   ("Error")`. Almost nothing returns that — tools report failures in their own
   words. So every graceful failure counted as a SUCCESS, and the escalation
   block that reads those counters (_tool_fail_counts, _rounds_without_success)
   has been unreachable for every failure except a registry exception. Dead
   code that looked live, which is this codebase's recurring shape.

2. Nothing stopped the model retrying, and each retry opened a permission
   prompt. A wrong password became a queue of dialogs asking the user to
   authorise something that could not happen — and the model drifted off task
   as it went, inventing a "Test Email" to "recipient@example.com".

The detector is the load-bearing part, and its false-positive direction matters
more than its true-positive one: a successful result misread as a failure would
suppress a tool that works.
"""
from __future__ import annotations

import pytest

from suni.core.orchestrator import _is_tool_failure, _REPEAT_FAIL_LIMIT


# ── real failure strings, taken from the tool modules ────────────────────────
@pytest.mark.parametrize("result", [
    "Failed to send email: (535, b'5.7.8 Error: authentication failed')",
    "Attachment(s) not found: /home/user/Desktop/coimbra_info.pdf",
    "Attachment(s) blocked — not in allowed directories or unsafe extension: x.exe",
    "PDF creation failed: [Errno 13] Permission denied",
    "Error: unknown tool 'frobnicate'. Available: ['create_pdf']",
    "Query error: no such column: nmae",
    "IMAP error: timed out",
    "Fetch error: too many redirects",
    "Download failed: 404",
    "Image generation failed: CUDA out of memory",
    "Memory search error: index corrupt",
    "Contact 'c-17' not found.",
    "Email UID 12 not found.",
    "User 'nobody' not found.",
    "Task 'abc' not found.",
    "Agent delegation is unavailable (no orchestrator bound).",
    "The agent 'researcher' failed: timeout",
    "The agent 'researcher' could not be loaded.",
    "Claude Code error (exit 1):\nboom",
    "Tool error (create_pdf): disk full",
])
def test_a_failure_is_recognised(result):
    assert _is_tool_failure(result), f"read as success: {result!r}"


# ── the direction that protects working tools ────────────────────────────────
@pytest.mark.parametrize("result", [
    "PDF created: C:/out/coimbra.pdf (33.0 KB)",
    "Email sent to a@example.com with 1 attachment(s): 'coimbra.pdf'.",
    "Saved.",
    "",
    "Found 3 results. The document says the file was not found in 1994.",
    "Search complete: 0 matches, nothing failed.",
    "Read 40 lines. Line 12 reads: Error: connection reset",
    "Deleted 2 entries.",
    "Wrote 1,204 bytes to notes.md",
    "The build succeeded after an earlier failed attempt.",
])
def test_a_success_is_not_mistaken_for_a_failure(result):
    """A false positive suppresses a tool that works — worse than missing a
    failure, which only costs an escalation that would not have helped."""
    assert not _is_tool_failure(result), f"read as failure: {result!r}"


def test_the_old_check_would_have_missed_almost_all_of_them():
    """Pins why this exists. If someone reverts to the prefix test, this fails
    with the count in the message rather than a silent behaviour change."""
    real_failures = [
        "Failed to send email: (535, auth failed)",
        "Attachment(s) not found: x.pdf",
        "PDF creation failed: disk full",
        "Query error: bad sql",
        "Contact 'c-1' not found.",
    ]
    missed = [r for r in real_failures if not r.startswith("Error")]
    assert len(missed) == len(real_failures), \
        "the old startswith('Error') check caught one of these after all"
    assert all(_is_tool_failure(r) for r in real_failures)


# ── the repeat guard ─────────────────────────────────────────────────────────
def test_the_limit_is_low_enough_to_matter():
    """Five prompts is what the user saw. The guard has to bite well before the
    iteration cap, or it changes nothing they would notice."""
    from suni.core.orchestrator import MAX_TOOL_ITERATIONS
    assert _REPEAT_FAIL_LIMIT == 2
    assert _REPEAT_FAIL_LIMIT < MAX_TOOL_ITERATIONS


def test_the_guard_runs_before_the_approval_gate():
    """The whole point. Suppressing the call after the gate would still ask the
    user to authorise an action SUNI has already decided not to perform."""
    import inspect
    from suni.core import orchestrator

    src = inspect.getsource(orchestrator.Orchestrator._agent_loop)
    guard = src.index("_tool_fail_total.get(tc.name, 0) >= _REPEAT_FAIL_LIMIT")
    gate = src.index("await _approval.request_approval(")
    assert guard < gate, "the repeat guard sits after the approval gate"


def test_the_guard_survives_a_tier_escalation():
    """Escalation clears _tool_fail_counts so a stronger model gets a clean
    slate. The guard must read the OTHER counter — otherwise each tier grants
    two more attempts and the prompt queue simply spreads across tiers."""
    import inspect
    from suni.core import orchestrator

    src = inspect.getsource(orchestrator.Orchestrator._agent_loop)
    assert "_tool_fail_total.get(tc.name, 0) >= _REPEAT_FAIL_LIMIT" in src
    # The reset on escalation must not touch the guard's counter.
    reset = src.index("_tool_fail_counts.clear()")
    window = src[reset:reset + 200]
    assert "_tool_fail_total.clear()" not in window, \
        "escalation resets the repeat guard, so every tier re-asks"


# ── language ─────────────────────────────────────────────────────────────────
def test_the_language_instruction_is_pinned_last():
    """Context.add() moves every SYSTEM message to the front when history
    overflows, and each tool round appends more English. A Portuguese request
    produced a Portuguese email subject and an English reply in the same run.
    The instruction is re-appended per inference so it lands last, which is the
    only position that survives both."""
    import inspect
    from suni.core import orchestrator

    src = inspect.getsource(orchestrator.Orchestrator._agent_loop)
    assert "_lang_pin" in src
    pin = src.index("_lang_pin = next(")
    call = src.index("response = await current_agent.chat(")
    assert pin < call, "the pin is computed after it is needed"
    assert "list(_msgs) + [_lang_pin]" in src, \
        "the instruction is not appended at the end of the message list"


def test_the_pin_does_not_grow_the_stored_history():
    """Appending to the message list, not to the context: anything added to the
    context is subject to the same relocation that buried it."""
    import inspect
    from suni.core import orchestrator

    src = inspect.getsource(orchestrator.Orchestrator._agent_loop)
    i = src.index("_lang_pin = next(")
    window = src[i:src.index("response = await current_agent.chat(")]
    assert "context.add(" not in window, \
        "the pin is added to the context, where trimming will relocate it"
