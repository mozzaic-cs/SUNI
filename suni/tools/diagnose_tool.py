"""Let her answer "what is wrong with you?" from her own evidence.

The afternoon this came from: an inbound Telegram message was received, logged,
routed — and never answered. No error, no reply, nothing. The cause was three
facts sitting in two log files (a turn with no completion, a model evicted for
want of VRAM, a tool schema larger than the window it was sent through), and
the only way anyone found them was a person reading logs over someone's
shoulder. The owner of the machine could not have.

So the report is the deliverable, in sentences, with the evidence attached. The
model's job is to relay it and answer follow-up questions about it — not to
invent a diagnosis of its own, because the findings are measured and a guess
dressed in the same paragraph is worse than no paragraph.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("suni.tools.diagnose")

# System internals: log excerpts, model placement, channel configuration. An
# inbound channel runs at "standard" precisely because it is not per-sender
# authenticated, and that is not somebody to hand the innards to.
_ALLOWED_ROLES = ("admin", "owner")

SCHEMA = {
    "name": "diagnose_self",
    "description": (
        "Check SUNI's own health and report what is wrong: turns that never "
        "finished, errors in the log, the state of the local model backend "
        "(evictions, GPU placement), whether the tool definitions fit the "
        "context window, and the messaging channels. Use when the user says "
        "something is broken, slow, stuck, or not replying — or asks how you "
        "are doing technically. Reads only; changes nothing."
    ),
    "parameters": {"type": "object", "properties": {}},
}


def handler() -> str:
    from ..tools.agent_tool import CURRENT_ROLE

    role = CURRENT_ROLE.get("standard")
    if role not in _ALLOWED_ROLES:
        return ("Self-diagnosis is for the machine's administrator. This "
                "conversation is not running with that role, so I will not "
                "read the logs out here.")
    try:
        from .. import diagnostics
        result = diagnostics.run()
        report = diagnostics.as_report(result)
    except Exception as exc:      # noqa: BLE001 — a failed check is not a crash
        _log.warning("[DIAGNOSE] failed: %s", exc)
        return f"I could not complete the self-check: {exc}"

    bad = [f for f in result["findings"] if f.level in ("broken", "slow")]
    _log.info("[DIAGNOSE] %d finding(s), %d worth acting on", len(result["findings"]), len(bad))
    return (
        report
        + "\n\nRelay this to the user in their own language, worst first, and "
        "keep the evidence with the finding it belongs to. These are measured, "
        "so do not add causes of your own alongside them — if they ask why, say "
        "what the evidence supports and what it does not. If nothing is wrong, "
        "say so plainly rather than hunting for something to report."
    )
