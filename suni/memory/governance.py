"""
The review queue: what happens to memory that was staged instead of approved.

`sensitivity.py` decides whether a fact may enter shared memory; this decides
what a human does with the ones it refused. Without it, staging is a hole
things fall into — a candidate is correctly unreadable, but also invisible
outside the audit trail, which is adequate while promotion is manual and
becomes a silent backlog the moment extraction is automatic.

Kept out of `server.py` deliberately. The promotion endpoint's logic can only
be tested by scanning its source for substrings, which is a weak test that
passes for the wrong reasons; the functions here are called directly by their
tests with a real store.

Approval is a **metadata transition, not a re-write**. The entry keeps its id,
its embedding and its provenance — so an approved candidate is traceable back
to who staged it and what the detector found, which is the point of recording
`detection` in the first place.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# A candidate is anything the gate declined to auto-approve. `rejected` is
# terminal and kept rather than deleted: "why is this fact not in memory" is a
# question with an answer only if the refusal survives.
PENDING = "candidate"
APPROVED = "approved"
REJECTED = "rejected"

POLICY_VERSION = "p1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _meta(entry: dict) -> dict:
    return entry.get("metadata") or {}


def list_candidates(store, include_rejected: bool = False) -> list[dict]:
    """Staged entries, newest first, without their embedding vectors.

    The content IS returned — a reviewer cannot judge a fact they cannot read,
    and this is an admin-only view of data an admin could already read from
    disk. What is deliberately not returned is the `e16` blob, which is large
    and unreadable.
    """
    wanted = {PENDING} | ({REJECTED} if include_rejected else set())
    out = []
    for e in store.get_all():
        m = _meta(e)
        if m.get("status") not in wanted:
            continue
        out.append({
            "id": e["id"],
            "content": e.get("content", ""),
            "type": e.get("type", "fact"),
            "timestamp": e.get("timestamp", ""),
            "status": m.get("status"),
            "visibility": m.get("visibility", "org"),
            "sensitivity": m.get("sensitivity", "normal"),
            "detection": m.get("detection", {}),
            "provenance": m.get("provenance", {}),
            "review": m.get("review", {}),
        })
    out.sort(key=lambda r: r["timestamp"], reverse=True)
    return out


def counts(store) -> dict:
    """Queue depth by status — for a badge, and for noticing a backlog."""
    tally = {PENDING: 0, APPROVED: 0, REJECTED: 0}
    for e in store.get_all():
        status = _meta(e).get("status", APPROVED)
        if status in tally:
            tally[status] += 1
    return tally


def _transition(store, memory_id: str, to_status: str, actor_id: str,
                actor_name: str, note: str = "",
                visibility: str | None = None) -> dict:
    entry = next((e for e in store.get_all() if e["id"] == memory_id), None)
    if entry is None:
        return {"ok": False, "reason": "not found"}

    m = _meta(entry)
    current = m.get("status", APPROVED)
    if current not in (PENDING, REJECTED):
        # Approving something already live, or re-deciding a settled entry, is
        # almost always a double-submit rather than an intention.
        return {"ok": False, "reason": f"not pending (status={current})"}
    if current == to_status:
        return {"ok": False, "reason": f"already {to_status}"}

    patch: dict[str, Any] = {
        "status": to_status,
        "review": {
            "approved_by": actor_id if to_status == APPROVED else "",
            "approved_at": _now() if to_status == APPROVED else "",
            "decided_by": actor_id,
            "decided_by_name": actor_name,
            "decided_at": _now(),
            "note": note,
            "policy_version": POLICY_VERSION,
        },
    }
    if visibility in ("org", "restricted"):
        patch["visibility"] = visibility

    if not store.update_metadata(memory_id, patch):
        return {"ok": False, "reason": "not found"}
    return {
        "ok": True,
        "id": memory_id,
        "status": to_status,
        "visibility": patch.get("visibility", m.get("visibility", "org")),
        "sensitivity": m.get("sensitivity", "normal"),
        "reasons": (m.get("detection") or {}).get("reasons", []),
    }


def approve(store, memory_id: str, actor_id: str, actor_name: str,
            note: str = "", visibility: str | None = None) -> dict:
    """Make a staged entry live.

    A reviewer may narrow visibility while approving — the common case is a
    fact that is fine to keep but should be `restricted` rather than `org`.
    Widening is equally possible and equally deliberate; the decision and who
    made it are recorded either way.
    """
    return _transition(store, memory_id, APPROVED, actor_id, actor_name,
                       note, visibility)


def reject(store, memory_id: str, actor_id: str, actor_name: str,
           note: str = "") -> dict:
    """Refuse a staged entry. It stays in the store, unreadable, as the record
    of a decision — the clearance predicate already excludes any status that is
    not `approved`."""
    return _transition(store, memory_id, REJECTED, actor_id, actor_name, note)
