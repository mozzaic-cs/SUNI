"""
Who may read which shared memory — the enterprise memory ACL.

This boundary decides whether a standard user can read a fact an admin marked
`restricted`. It was implemented (docs/enterprise-memory-governance.md, Phase 1)
and shipped with no tests at all, which in this codebase is not a safe place to
be: the same session that noticed found a scope filter that reached nothing, a
parity test comparing a constant to itself, and a fixed-window assertion that had
silently stopped testing what it named.

The subtle property, and the reason `scope` is a *candidate-stage* predicate
rather than a filter over results: filtering the returned top-k would silently
drop in-clearance entries whenever out-of-clearance entries scored higher. The
ACL would look correct — nothing leaks — while quietly hiding memory the caller
was entitled to. `test_filtering_happens_before_top_k_not_after` is the one that
would catch a regression there, and it is written so it FAILS under the wrong
implementation rather than passing by luck.
"""
from __future__ import annotations

import numpy as np
import pytest

from suni import rbac
from suni.memory.manager import _clearance_scope
from suni.memory.store import MemoryStore


def _entry(status: str | None = "approved", visibility: str | None = "org") -> dict:
    meta: dict = {}
    if status is not None:
        meta["status"] = status
    if visibility is not None:
        meta["visibility"] = visibility
    return {"metadata": meta}


# ── the matrix ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role,expected", [
    ("read-only", {"org"}),
    ("standard", {"org"}),
    ("power-user", {"org", "restricted"}),
    ("admin", {"org", "restricted"}),
])
def test_clearance_per_role(role, expected):
    assert rbac.clearance_for_role(role) == expected


@pytest.mark.parametrize("role", ["read-only", "standard"])
def test_ordinary_users_cannot_read_restricted_memory(role):
    pred = _clearance_scope(rbac.clearance_for_role(role))
    assert pred(_entry(visibility="org")) is True
    assert pred(_entry(visibility="restricted")) is False


@pytest.mark.parametrize("role", ["power-user", "admin"])
def test_privileged_roles_can_read_restricted_memory(role):
    pred = _clearance_scope(rbac.clearance_for_role(role))
    assert pred(_entry(visibility="restricted")) is True


@pytest.mark.parametrize("status", ["candidate", "rejected", "redacted", "pending"])
def test_only_approved_entries_are_ever_readable(status):
    """A fact awaiting review must not be queryable by anyone, including admin —
    the gate is the point of the pipeline."""
    for role in ("read-only", "standard", "power-user", "admin"):
        pred = _clearance_scope(rbac.clearance_for_role(role))
        assert pred(_entry(status=status, visibility="org")) is False


def test_an_unknown_visibility_label_is_not_readable():
    """A typo or a label from a future phase must fail closed, not open."""
    pred = _clearance_scope({"org", "restricted"})
    assert pred(_entry(visibility="dept:finance")) is False
    assert pred(_entry(visibility="")) is False


# ── fail-safe defaults ───────────────────────────────────────────────────────
def test_missing_clearance_falls_back_to_org_never_wide_open():
    """A caller that forgot to pass clearance must not receive restricted data."""
    for missing in (None, set()):
        pred = _clearance_scope(missing)
        assert pred(_entry(visibility="org")) is True
        assert pred(_entry(visibility="restricted")) is False


def test_legacy_entries_stay_readable_without_migration():
    """Pre-governance collective entries carry no metadata. They default to
    approved/org so shipping the feature did not silently hide existing data."""
    pred = _clearance_scope({"org"})
    assert pred({"metadata": {}}) is True
    assert pred({}) is True
    assert pred({"metadata": {"status": "approved"}}) is True   # visibility absent
    assert pred({"metadata": {"visibility": "org"}}) is True    # status absent


# ── the correctness property that is easy to get wrong ───────────────────────
def test_filtering_happens_before_top_k_not_after(tmp_path):
    """Filtering results instead of candidates hides in-clearance memory.

    Ten restricted entries are made to score HIGHER than the one org entry.
    With candidate-stage filtering a standard user still gets the org entry.
    Filtering the top-k afterwards would return nothing at all — and look like
    'no relevant memory' rather than a bug.
    """
    store = MemoryStore(str(tmp_path / "collective.json"))
    dim = 8
    query = [1.0] + [0.0] * (dim - 1)

    for i in range(10):
        store.add(f"restricted secret {i}", list(np.array(query, dtype=np.float32)),
                  "fact", metadata={"status": "approved", "visibility": "restricted"})
    # deliberately a weaker match than the restricted ones
    weaker = [0.6, 0.8] + [0.0] * (dim - 2)
    store.add("org-wide fact", weaker, "fact",
              metadata={"status": "approved", "visibility": "org"})

    pred = _clearance_scope({"org"})
    hits = store.search(query, top_k=5, threshold=0.3, scope=pred)

    assert hits, "in-clearance memory was dropped — filtering ran after top-k"
    assert all(h["content"] == "org-wide fact" for h in hits)
    assert not any("restricted" in h["content"] for h in hits)


def test_the_predicate_is_applied_to_candidates_in_the_source():
    """Belt and braces: the ordering above could pass by luck on a small set."""
    import inspect
    src = inspect.getsource(MemoryStore.search)
    i_scope = src.index("scope(e)")
    i_topk = src.index("argsort")
    assert i_scope < i_topk, "scope is applied after ranking"


# ── the private store is never scoped ────────────────────────────────────────
def test_a_users_own_memory_is_never_filtered_by_clearance(tmp_path):
    """Clearance governs the COLLECTIVE store only. Applying it to a private
    store would make a read-only user unable to read their own memory."""
    import inspect
    from suni.memory.manager import MemoryManager
    src = inspect.getsource(MemoryManager.build_context)
    i_self = src.index("self.store.search")
    call = src[i_self:i_self + 200]
    assert "scope=" not in call, "the user's private store is being clearance-filtered"


def test_default_search_is_unfiltered(tmp_path):
    """scope=None must leave existing per-user stores byte-for-byte unchanged."""
    store = MemoryStore(str(tmp_path / "private.json"))
    vec = [1.0, 0.0, 0.0, 0.0]
    store.add("a private thought", vec, "fact")
    assert store.search(vec, top_k=5, threshold=0.3)


# ── the promotion gate ───────────────────────────────────────────────────────
def _promote_block() -> str:
    """The whole endpoint, sliced to the next route rather than a fixed length.

    This used to take a fixed 2600 characters and silently stopped covering the
    approval branch the moment the endpoint grew — the same way a 4000-character
    window in test_updater.py quietly stopped testing mark_busy. A window that
    can fall short of what it names is not a test, it is a coin flip.
    """
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    srv = (root / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    i = srv.index('@app.post("/api/memory/promote")')
    nxt = re.search(r'\n    @app\.', srv[i + 10:])
    return srv[i:i + 10 + nxt.start()] if nxt else srv[i:]


def test_promotion_requires_power_user_or_admin():
    block = _promote_block()
    assert 'role") not in ("admin", "power-user")' in block
    assert "403" in block


def test_promotion_validates_the_visibility_label():
    """An arbitrary label would create memory nobody can read, or worse, one
    that slips past the clearance check."""
    block = _promote_block()
    assert 'visibility not in ("org", "restricted")' in block


def test_promotion_dedupes_before_writing():
    block = _promote_block()
    assert "threshold=0.93" in block and "duplicate" in block


def test_promotion_records_provenance_and_review():
    """Who promoted it, when, and under which policy — the compliance story."""
    block = _promote_block()
    for field in ("visibility", "sensitivity", "status", "provenance", "review",
                  "policy_version", "source_user_id", "approved_by"):
        assert field in block, f"promotion metadata is missing {field}"


def test_both_promotion_outcomes_are_audited():
    """Only auditing successes leaves 'why was this rejected' unanswerable."""
    block = _promote_block()
    assert "memory.promote.approved" in block
    assert "memory.promote.rejected" in block


# ── the wiring ───────────────────────────────────────────────────────────────
def test_clearance_actually_reaches_the_collective_search():
    """The recurring defect in this codebase is a setting that reaches nothing.
    An ACL computed and then not passed through would be exactly that."""
    import inspect
    from suni.memory.manager import MemoryManager
    src = inspect.getsource(MemoryManager.build_context)
    i = src.index("collective_store.search")
    assert "scope=_clearance_scope(clearance)" in src[i:i + 240]


def test_the_orchestrator_supplies_the_callers_clearance():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    src = (root / "suni" / "core" / "orchestrator.py").read_text(encoding="utf-8")
    assert "clearance_for_role(user_role)" in src
    i = src.index("clearance_for_role(user_role)")
    assert "clearance=" in src[i:i + 400], "clearance is computed but never passed"
