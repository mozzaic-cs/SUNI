"""Why a run exists, carried to the run that has to do the work.

A delegated agent and a scheduled job both start from a bare instruction in a
fresh context. The specialist is handed "summarise the Q3 numbers" with no idea
that the person asked for a board pack, and a job that fires every Monday is
amnesiac by construction: it cannot tell a first attempt from the fourth, or
know that last week's run failed.

The note is CONTEXT, not instruction. It is added as a system message, phrased
as background, and says plainly that it must not be repeated back — a run that
narrates its own plumbing at the user is the failure this codebase has already
had once (see _task_suffix in the Claude Code agent).
"""
from __future__ import annotations

from contextvars import ContextVar

# The request that started the CURRENT turn, set by the orchestrator so anything
# it delegates to can say what the work is ultimately for. Task-local: two users
# delegating at once never see each other's.
CURRENT_REQUEST: ContextVar[str] = ContextVar("suni_current_request", default="")

_MAX = 400          # a note is context, not a transcript


def _trim(text: str, limit: int = _MAX) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def delegation_note(origin: str, agent_name: str = "") -> str:
    """Background for a sub-agent: what the person actually asked for.

    Empty when there is no originating request — a note that says nothing costs
    tokens and invites the model to invent a reason.
    """
    origin = _trim(origin)
    if not origin:
        return ""
    who = f" to {agent_name}" if agent_name else ""
    return (
        "[Background — why this was delegated"
        f"{who}: the person's request was: \"{origin}\". "
        "Answer the instruction you were given; use this only to judge what "
        "matters. Do not quote or mention this note.]"
    )


def schedule_note(name: str, cadence: str, last_status: str = "",
                  last_run: str = "") -> str:
    """Background for a scheduled run: that it recurs, and how the last one went.

    The previous status is the useful half. Without it a job that has failed
    four times in a row starts the fifth attempt believing it is the first.
    """
    name = _trim(name, 80)
    if not name:
        return ""
    bits = [f"this is a recurring job called \"{name}\""]
    if cadence:
        bits.append(f"it runs {_trim(cadence, 60)}")
    status = _trim(last_status, 120)
    if status:
        when = f" on {_trim(last_run, 40)}" if last_run else ""
        if status.lower().startswith(("error", "failed", "skipped")):
            bits.append(f"the previous run{when} did not succeed: {status}")
        else:
            bits.append(f"the previous run{when} finished: {status}")
    else:
        bits.append("this is its first run")
    return ("[Background — " + "; ".join(bits)
            + ". Nobody is watching this run, so do not ask questions you cannot "
              "get answered. Do not quote or mention this note.]")
