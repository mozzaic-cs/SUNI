"""
The review queue — what a human does with memory the gate refused.

These call the governance functions directly against a real store, rather than
scanning `server.py` for substrings the way the promotion tests have to. A
source-scan test passes when the string is present and says nothing about
whether the transition works; several tests in this repo have quietly stopped
covering what they named for exactly that reason.

The property that matters most is that approval is a metadata transition and
nothing else: the entry keeps its id, its embedding and its provenance, so an
approved fact remains traceable to whoever staged it and to what the detector
found. Re-writing it would lose the audit chain at the moment it starts to
matter.
"""
from __future__ import annotations

import pytest

from suni.memory import governance as gov
from suni.memory.manager import _clearance_scope
from suni.memory.store import MemoryStore

VEC = [1.0, 0.0, 0.0, 0.0]


@pytest.fixture()
def store(tmp_path):
    s = MemoryStore(str(tmp_path / "collective.json"))
    s.add("staged pii fact", VEC, "fact", metadata={
        "status": "candidate", "visibility": "org", "sensitivity": "pii",
        "detection": {"reasons": ["pt-nif"], "injection": False},
        "provenance": {"source_type": "manual", "source_user_id": "u1"},
    })
    s.add("already live fact", VEC, "fact", metadata={
        "status": "approved", "visibility": "org", "sensitivity": "normal"})
    return s


def _ids(store, status):
    return [e["id"] for e in store.get_all()
            if (e.get("metadata") or {}).get("status") == status]


# ── the queue shows what is waiting ──────────────────────────────────────────
def test_only_candidates_are_listed(store):
    rows = gov.list_candidates(store)
    assert len(rows) == 1
    assert rows[0]["content"] == "staged pii fact"
    assert rows[0]["sensitivity"] == "pii"
    assert rows[0]["detection"]["reasons"] == ["pt-nif"]


def test_the_queue_does_not_return_embedding_blobs(store):
    """Large, unreadable, and of no use to a reviewer."""
    rows = gov.list_candidates(store)
    assert "e16" not in rows[0] and "embedding" not in rows[0]


def test_counts_report_queue_depth(store):
    c = gov.counts(store)
    assert c["candidate"] == 1 and c["approved"] == 1 and c["rejected"] == 0


def test_rejected_entries_are_hidden_unless_asked_for(store):
    cid = _ids(store, "candidate")[0]
    gov.reject(store, cid, "u2", "alex", note="contains a client NIF")
    assert gov.list_candidates(store) == []
    assert len(gov.list_candidates(store, include_rejected=True)) == 1


# ── approving actually changes what can be read ──────────────────────────────
def test_approving_makes_the_entry_readable(store):
    cid = _ids(store, "candidate")[0]
    entry = next(e for e in store.get_all() if e["id"] == cid)
    pred = _clearance_scope({"org", "restricted"})
    assert pred(entry) is False, "a candidate should be unreadable to start with"

    r = gov.approve(store, cid, "u2", "alex")
    assert r["ok"] is True

    entry = next(e for e in store.get_all() if e["id"] == cid)
    assert pred(entry) is True, "approval did not make the entry readable"


def test_rejecting_leaves_it_unreadable_and_kept(store):
    """Kept rather than deleted: 'why is this fact not in memory' only has an
    answer if the refusal survives."""
    cid = _ids(store, "candidate")[0]
    gov.reject(store, cid, "u2", "alex", note="client identifier")
    entry = next(e for e in store.get_all() if e["id"] == cid)
    assert _clearance_scope({"org", "restricted"})(entry) is False
    assert entry["metadata"]["review"]["note"] == "client identifier"
    assert store.count() == 2, "the rejected entry was deleted"


def test_approval_preserves_id_embedding_and_provenance(store):
    cid = _ids(store, "candidate")[0]
    before = next(e for e in store.get_all() if e["id"] == cid)
    blob, prov = before["e16"], before["metadata"]["provenance"]

    gov.approve(store, cid, "u2", "alex")

    after = next(e for e in store.get_all() if e["id"] == cid)
    assert after["id"] == cid
    assert after["e16"] == blob, "the embedding was rewritten"
    assert after["metadata"]["provenance"] == prov, "provenance was lost"
    assert after["metadata"]["detection"]["reasons"] == ["pt-nif"], \
        "the detector's findings were dropped on approval"


def test_who_decided_is_recorded(store):
    cid = _ids(store, "candidate")[0]
    gov.approve(store, cid, "u2", "alex", note="ok, internal only")
    review = next(e for e in store.get_all() if e["id"] == cid)["metadata"]["review"]
    assert review["decided_by"] == "u2" and review["decided_by_name"] == "alex"
    assert review["approved_by"] == "u2" and review["approved_at"]
    assert review["note"] == "ok, internal only"
    assert review["policy_version"] == gov.POLICY_VERSION


def test_a_reviewer_can_narrow_visibility_while_approving(store):
    """The common case: a fact worth keeping, but not org-wide."""
    cid = _ids(store, "candidate")[0]
    gov.approve(store, cid, "u2", "alex", visibility="restricted")
    entry = next(e for e in store.get_all() if e["id"] == cid)
    assert entry["metadata"]["visibility"] == "restricted"
    assert _clearance_scope({"org"})(entry) is False
    assert _clearance_scope({"org", "restricted"})(entry) is True


def test_an_invalid_visibility_is_ignored_not_applied(store):
    cid = _ids(store, "candidate")[0]
    gov.approve(store, cid, "u2", "alex", visibility="everyone")
    entry = next(e for e in store.get_all() if e["id"] == cid)
    assert entry["metadata"]["visibility"] == "org", "an unknown label was written"


# ── refusals ─────────────────────────────────────────────────────────────────
def test_an_unknown_id_is_refused(store):
    r = gov.approve(store, "no-such-id", "u2", "alex")
    assert r["ok"] is False and r["reason"] == "not found"


def test_an_already_live_entry_cannot_be_re_approved(store):
    """Almost always a double-submit rather than an intention."""
    live = _ids(store, "approved")[0]
    r = gov.approve(store, live, "u2", "alex")
    assert r["ok"] is False and "not pending" in r["reason"]


def test_a_second_decision_on_the_same_candidate_is_refused(store):
    cid = _ids(store, "candidate")[0]
    assert gov.approve(store, cid, "u2", "alex")["ok"] is True
    again = gov.approve(store, cid, "u3", "sam")
    assert again["ok"] is False, "a settled entry was decided twice"


def test_a_rejected_entry_can_still_be_approved_later(store):
    """A reviewer who rejects in error must not be stuck with it."""
    cid = _ids(store, "candidate")[0]
    gov.reject(store, cid, "u2", "alex")
    assert gov.approve(store, cid, "u2", "alex")["ok"] is True


# ── the store primitive it rests on ──────────────────────────────────────────
def test_update_metadata_merges_rather_than_replaces(tmp_path):
    s = MemoryStore(str(tmp_path / "m.json"))
    mid = s.add("x", VEC, "fact", metadata={"a": 1, "b": 2})
    assert s.update_metadata(mid, {"b": 3, "c": 4}) is True
    meta = s.get_all()[0]["metadata"]
    assert meta == {"a": 1, "b": 3, "c": 4}, "existing keys were dropped"


def test_update_metadata_reports_an_unknown_id(tmp_path):
    s = MemoryStore(str(tmp_path / "m.json"))
    assert s.update_metadata("nope", {"status": "approved"}) is False


def test_update_metadata_survives_a_reload(tmp_path):
    """It must persist, not just mutate the in-memory list."""
    path = str(tmp_path / "m.json")
    s = MemoryStore(path)
    mid = s.add("x", VEC, "fact", metadata={"status": "candidate"})
    s.update_metadata(mid, {"status": "approved"})
    assert MemoryStore(path).get_all()[0]["metadata"]["status"] == "approved"


def test_update_metadata_takes_the_lock(tmp_path):
    """_save() rewrites the whole file from self._data; an unsynchronised
    writer loses a concurrent change rather than merging it."""
    import inspect
    assert "self._lock" in inspect.getsource(MemoryStore.update_metadata)


# ── the endpoints ────────────────────────────────────────────────────────────
def _server_src() -> str:
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    return (root / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")


@pytest.mark.parametrize("route", [
    '@app.get("/api/memory/candidates")',
    '@app.post("/api/memory/candidates/{memory_id}/approve")',
    '@app.post("/api/memory/candidates/{memory_id}/reject")',
])
def test_review_routes_are_reviewer_only(route):
    src = _server_src()
    i = src.index(route)
    assert "_require_reviewer" in src[i:i + 700], f"{route} is not gated"


def test_review_decisions_are_audited_without_echoing_content():
    """The entry was staged because of what it contains; putting that in the
    audit trail would defeat the staging."""
    src = _server_src()
    for action in ("memory.promote.approved", "memory.promote.rejected"):
        i = src.index(f'"{action}",\n', src.index("_require_reviewer"))
        block = src[i:i + 400]
        assert "reasons" in block
        assert "content" not in block, "review audit echoes the staged content"


# ── the admin panel ──────────────────────────────────────────────────────────
def _html() -> str:
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    return (root / "suni" / "web" / "admin.html").read_text(encoding="utf-8")


def test_the_panel_is_wired_end_to_end():
    """Five separate things, and a panel is invisible if any one is missing —
    this file has shipped a nav tab with no panel and a card in the wrong
    container before."""
    html = _html()
    assert "switchTab('memory',this)" in html, "no nav tab"
    assert 'id="panel-memory"' in html, "no panel div"
    assert "if (name === 'memory') loadMemoryQueue()" in html, "tab loads nothing"
    assert "async function loadMemoryQueue" in html, "no loader"
    assert "async function memqDecide" in html, "no decision handler"


def test_the_panel_is_a_top_level_panel_not_nested():
    """A panel nested inside another never becomes visible — switchTab toggles
    .active on the outer one only, which is how a card once landed inside
    <div class="hs-line"> and vanished.

    Counts real div depth up to the panel's opening tag. An earlier version of
    this test compared a count to itself and ended in `or True`, so it passed
    for every possible input — the vacuous-guard pattern this suite keeps
    finding elsewhere.
    """
    import re as _re
    html = _html()

    def _depth_at(idx: int) -> int:
        d = 0
        for m in _re.finditer(r"<div\b|</div>", html[:idx]):
            d += 1 if m.group(0) == "<div" else -1
        return d

    depths = {pid: _depth_at(html.index(f'<div class="panel" id="panel-{pid}">'))
              for pid in ("users", "agents", "skills", "memory")}
    siblings = {v for k, v in depths.items() if k != "memory"}
    assert len(siblings) == 1, f"the existing panels disagree on depth: {depths}"
    assert depths["memory"] == siblings.pop(), (
        f"panel-memory sits at a different div depth from its siblings: {depths}")


def test_the_queue_reads_the_endpoint_it_was_given():
    html = _html()
    i = html.index("async function loadMemoryQueue")
    block = html[i:i + 1400]
    assert "/api/memory/candidates" in block
    assert "include_rejected=" in block


def test_a_decision_asks_for_a_reason():
    """The note is what answers 'why is this fact not in memory' later, and the
    decision is recorded against a person."""
    html = _html()
    i = html.index("async function memqDecide")
    block = html[i:i + 900]
    assert "prompt(" in block, "a decision fires on one click with no reason"
    assert "note" in block


def test_every_new_key_exists_in_both_languages():
    import pathlib
    import re as _re
    root = pathlib.Path(__file__).resolve().parent.parent
    js = (root / "suni" / "web" / "i18n.js").read_text(encoding="utf-8")
    html = _html()
    used = set(_re.findall(r"""(?:data-i18n(?:-html)?=")(admin\.memq[a-z_]*|admin\.nav_memory)""", html))
    used |= set(_re.findall(r"""t\('(admin\.memq[a-z_]*)'""", html))
    assert used, "no memory-queue keys found in the panel"
    for key in used:
        assert js.count(f'"{key}":') == 2, f"{key} is missing from en or pt"


def test_every_sensitivity_the_detector_emits_has_a_label():
    """A missing key renders as the raw key in the badge — and the levels come
    from sensitivity.LEVELS, so adding one there must not silently produce
    `admin.sens_whatever` in the panel."""
    import pathlib
    from suni.sensitivity import LEVELS
    root = pathlib.Path(__file__).resolve().parent.parent
    js = (root / "suni" / "web" / "i18n.js").read_text(encoding="utf-8")
    for level in LEVELS:
        assert js.count(f'"admin.sens_{level}":') == 2, \
            f"sensitivity '{level}' has no label in en and pt"


def test_the_badge_is_translated_not_raw():
    html = _html()
    i = html.index("async function loadMemoryQueue")
    block = html[i:i + 1600]
    assert "t('admin.sens_'" in block, "the sensitivity badge shows the raw value"
