"""Tell the interface which agent is working, so it can be shown.

An agent call was invisible. A delegation, a fan-out to several specialists or
an agent picked for the turn all ran inside one "thinking" state, and the only
trace was a line of subtitle. The Face now gives each agent a figure of its own
(not SUNI's likeness), and needs to know when one starts, when it is waiting on
a person, and when it is done.

One event, sent from one place: Orchestrator.run, whenever it runs under an
agent profile. Every way of calling an agent ends up there, so none of them can
forget to say so.

Fan-out and invoke_agent sub-runs are given no event stream on purpose — a
specialist's tokens would otherwise stream to the user as if SUNI were saying
them. Presence goes to the OUTER turn's stream instead, which the sub-run still
inherits through EVENT_CB_CTX. Only the presence event goes there; nothing else
from the sub-run does.
"""
from __future__ import annotations

from contextvars import ContextVar

# The agent the current (sub-)run is acting as, so an approval card raised
# inside it can say whose request it is.
CURRENT_AGENT: ContextVar[str] = ContextVar("suni_current_agent", default="")


def describe(profile: dict | None) -> dict:
    p = profile or {}
    return {"slug": str(p.get("slug") or ""),
            "name": str(p.get("name") or p.get("slug") or "agent")}


def emit(event_cb, phase: str, profile: dict | None, detail: str = "") -> None:
    """phase: start | done | error. Never raises — a figure on a screen is not
    worth a failed turn."""
    if not event_cb or not profile:
        return
    try:
        evt = {"type": "agent", "phase": phase, **describe(profile)}
        if detail:
            evt["detail"] = detail[:200]
        event_cb(evt)
    except Exception:      # noqa: BLE001
        pass
