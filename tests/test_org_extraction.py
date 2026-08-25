"""
Turning one person's conversation into memory other people can read.

This is the riskiest path into shared memory in the system: nobody reviewed the
conversation it came from, and the extraction is a 7B model's judgement about
what counts as "organisational". So the tests are mostly about restraint —
that it is off by default, that off is genuinely off, and that nothing it
produces is ever published without a human.

The dedup test matters more than it looks. Consolidation runs weekly, so a fact
that gets re-staged every pass turns the review queue into an argument with the
machine, and the reviewer stops reading it — the same way a noisy secret scanner
gets ignored.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from suni.memory import consolidator as cons
from suni.memory.store import MemoryStore

VEC = [0.1] * 8


class _Manager:
    """Enough MemoryManager for extraction: a private store, a collective store,
    and a deterministic embedder."""

    def __init__(self, tmp_path, uid="11111111-1111-1111-1111-111111111111",
                 collective=True):
        udir = tmp_path / "memory" / "users" / uid
        udir.mkdir(parents=True)
        self.store = MemoryStore(str(udir / "suni_memory.json"))
        self.collective_store = (
            MemoryStore(str(tmp_path / "memory" / "collective_memory.json"))
            if collective else None)
        self.added: list[tuple[str, str]] = []

    async def _embed(self, text: str):
        # Distinct vectors per text so dedup is exercised honestly rather than
        # everything colliding at cosine 1.0.
        h = sum(ord(c) for c in text)
        return [((h >> i) & 7) / 7 or 0.01 for i in range(8)]

    def _embed_write_ok(self, embedding):
        return True

    async def add(self, content, memory_type=None, metadata=None):
        self.added.append((content, memory_type))
        return "id"


def _seed_conversation(mgr):
    mgr.store.add("user: we signed ACME as our new supplier", VEC, "conversation")


def _run(mgr, llm_output, monkeypatch, org_on):
    monkeypatch.setattr(cons, "_call_ollama",
                        lambda *a, **k: _async(llm_output))
    monkeypatch.setattr(cons, "_cfg",
                        lambda key, fallback: org_on
                        if key == "memory_org_extraction" else fallback)
    return asyncio.run(cons.extract_facts(mgr))


def _async(value):
    async def _c():
        return value
    return _c()


# ── off by default, and off means off ────────────────────────────────────────
def test_it_is_off_by_default():
    from suni import config
    assert config.DEFAULTS["memory_org_extraction"] is False, (
        "org extraction turns private conversation into shared memory; it must "
        "not switch itself on at upgrade")


def test_with_the_feature_off_the_model_is_never_told_about_org(tmp_path, monkeypatch):
    """Off is byte-identical to the behaviour before this existed: the marker is
    not in the prompt, so the model cannot emit it."""
    seen = {}

    def _spy(prompt, system, host, model):
        seen["system"] = system
        return _async("[FACT] the user likes tea")

    mgr = _Manager(tmp_path)
    _seed_conversation(mgr)
    monkeypatch.setattr(cons, "_call_ollama", _spy)
    monkeypatch.setattr(cons, "_cfg", lambda key, fb: False
                        if key == "memory_org_extraction" else fb)
    asyncio.run(cons.extract_facts(mgr))
    assert "[ORG]" not in seen["system"]
    assert seen["system"] == cons._EXTRACT_SYSTEM


def test_with_the_feature_on_the_rules_are_added(tmp_path, monkeypatch):
    seen = {}

    def _spy(prompt, system, host, model):
        seen["system"] = system
        return _async("")

    mgr = _Manager(tmp_path)
    _seed_conversation(mgr)
    monkeypatch.setattr(cons, "_call_ollama", _spy)
    monkeypatch.setattr(cons, "_cfg", lambda key, fb: True
                        if key == "memory_org_extraction" else fb)
    asyncio.run(cons.extract_facts(mgr))
    assert "[ORG]" in seen["system"]
    assert "never [ORG]" in seen["system"], "the personal/org distinction is unstated"


def test_an_org_line_with_the_feature_off_becomes_a_personal_fact(tmp_path, monkeypatch):
    """A model that emits the marker anyway must not have the line dropped."""
    mgr = _Manager(tmp_path)
    _seed_conversation(mgr)
    _run(mgr, "[ORG] ACME is the new supplier", monkeypatch, org_on=False)
    assert mgr.added == [("ACME is the new supplier", "fact")]
    assert mgr.collective_store.count() == 0, "it leaked into shared memory"


def test_nothing_is_staged_when_there_is_no_collective_store(tmp_path, monkeypatch):
    mgr = _Manager(tmp_path, collective=False)
    _seed_conversation(mgr)
    _run(mgr, "[ORG] ACME is the new supplier", monkeypatch, org_on=True)
    assert mgr.added and mgr.added[0][1] == "fact", "the fact was lost entirely"


# ── what staging produces ────────────────────────────────────────────────────
def test_an_org_fact_is_staged_never_published(tmp_path, monkeypatch):
    """Even a clean fact waits for a human — §9.2, default-deny while the
    detector is young. `normal` here means 'no findings', not 'safe to share'."""
    mgr = _Manager(tmp_path)
    _seed_conversation(mgr)
    _run(mgr, "[ORG] ACME is the new supplier from October", monkeypatch, org_on=True)

    entries = mgr.collective_store.get_all()
    assert len(entries) == 1
    meta = entries[0]["metadata"]
    assert meta["status"] == "candidate", "an extracted fact was published"
    assert meta["sensitivity"] == "normal"
    assert meta["review"]["approved_by"] == ""
    assert mgr.added == [], "an org fact also became a personal fact"


def test_a_staged_fact_is_unreadable_until_reviewed(tmp_path, monkeypatch):
    from suni.memory.manager import _clearance_scope
    mgr = _Manager(tmp_path)
    _seed_conversation(mgr)
    _run(mgr, "[ORG] ACME is the new supplier", monkeypatch, org_on=True)
    entry = mgr.collective_store.get_all()[0]
    assert _clearance_scope({"org", "restricted"})(entry) is False


def test_provenance_records_whose_conversation_it_came_from(tmp_path, monkeypatch):
    """Without this the candidate is unattributable, and erasure can no more
    reach it than it can the legacy entries — the gap this phase should be
    closing, not widening."""
    uid = "22222222-2222-2222-2222-222222222222"
    mgr = _Manager(tmp_path, uid=uid)
    _seed_conversation(mgr)
    _run(mgr, "[ORG] ACME is the new supplier", monkeypatch, org_on=True)
    prov = mgr.collective_store.get_all()[0]["metadata"]["provenance"]
    assert prov["source_user_id"] == uid, f"owner not derived: {prov}"
    assert prov["source_type"] == "extracted"


def test_the_detector_still_labels_extracted_content(tmp_path, monkeypatch):
    mgr = _Manager(tmp_path)
    _seed_conversation(mgr)
    _run(mgr, "[ORG] Billing contact is maria.silva@example.pt",
         monkeypatch, org_on=True)
    meta = mgr.collective_store.get_all()[0]["metadata"]
    assert meta["sensitivity"] == "pii"
    assert "email-address" in meta["detection"]["reasons"]
    assert meta["status"] == "candidate"


# ── the weekly-repeat problem ────────────────────────────────────────────────
def test_the_same_fact_is_not_re_staged_every_week(tmp_path, monkeypatch):
    """Consolidation runs weekly. A queue that regrows the same rows is one the
    reviewer stops reading."""
    mgr = _Manager(tmp_path)
    _seed_conversation(mgr)
    line = "[ORG] ACME is the new supplier from October"
    _run(mgr, line, monkeypatch, org_on=True)
    assert mgr.collective_store.count() == 1
    _run(mgr, line, monkeypatch, org_on=True)
    assert mgr.collective_store.count() == 1, "the same fact was staged twice"


def test_a_rejected_fact_is_not_re_staged(tmp_path, monkeypatch):
    """Re-proposing something a human already refused would make the queue an
    argument with the machine."""
    from suni.memory import governance as gov
    mgr = _Manager(tmp_path)
    _seed_conversation(mgr)
    line = "[ORG] ACME is the new supplier from October"
    _run(mgr, line, monkeypatch, org_on=True)
    cid = mgr.collective_store.get_all()[0]["id"]
    gov.reject(mgr.collective_store, cid, "u1", "alex", note="not shareable")

    _run(mgr, line, monkeypatch, org_on=True)
    assert mgr.collective_store.count() == 1, "a rejected fact came back"


# ── mixed output ─────────────────────────────────────────────────────────────
def test_personal_and_org_lines_are_routed_separately(tmp_path, monkeypatch):
    mgr = _Manager(tmp_path)
    _seed_conversation(mgr)
    n = _run(mgr, "[FACT] the user is based in Porto\n"
                  "[PREFERENCE] the user prefers European Portuguese\n"
                  "[ORG] ACME is the new supplier",
             monkeypatch, org_on=True)
    assert n == 3
    assert [t for _, t in mgr.added] == ["fact", "preference"]
    assert mgr.collective_store.count() == 1


def test_the_owner_is_blank_for_a_non_user_store(tmp_path):
    """The shared store is not under memory/users/<id>/, so there is no owner
    to claim — better blank than wrong."""
    mgr = _Manager(tmp_path)
    mgr.store = MemoryStore(str(tmp_path / "memory" / "suni_memory.json"))
    assert cons._owner_of(mgr) == ""


# ── the erasure consequence ──────────────────────────────────────────────────
def test_an_extracted_candidate_is_erasable(tmp_path):
    """Extraction stamps provenance, so a person's extracted facts leave the
    shared store with them. Before this, they would have stayed in memory
    colleagues can read after the subject was told the erasure was complete."""
    import json
    from suni import erasure
    uid = "33333333-3333-3333-3333-333333333333"
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "collective_memory.json").write_text(json.dumps([
        {"id": "a", "content": "from the subject", "metadata": {
            "status": "candidate",
            "provenance": {"source_type": "extracted", "source_user_id": uid}}},
        {"id": "b", "content": "from someone else", "metadata": {
            "provenance": {"source_type": "extracted", "source_user_id": "other"}}},
        {"id": "c", "content": "legacy, unattributed", "metadata": {}},
    ]), encoding="utf-8")

    erasure.erase(uid, uid, root=tmp_path)

    left = json.loads((mem / "collective_memory.json").read_text(encoding="utf-8"))
    ids = {e["id"] for e in left}
    assert "a" not in ids, "the subject's extracted fact survived erasure"
    assert ids == {"b", "c"}, "erasure removed someone else's data"


def test_the_preview_counts_only_what_it_cannot_reach(tmp_path):
    """Reporting the whole shared store as unerasable becomes less true every
    week once extraction is on."""
    import json
    from suni import erasure
    uid = "44444444-4444-4444-4444-444444444444"
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "collective_memory.json").write_text(json.dumps([
        {"id": "a", "metadata": {"provenance": {"source_user_id": uid}}},
        {"id": "b", "metadata": {}},
        {"id": "c", "metadata": {}},
    ]), encoding="utf-8")

    rows = {u["store"]: u for u in erasure.unattributed_stores(tmp_path)}
    row = rows["memory/collective_memory.json"]
    assert row["entries"] == 2, f"attributed entries counted as unreachable: {row}"
    assert "ARE erased" in row["reason"]
