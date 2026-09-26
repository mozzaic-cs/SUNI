"""The map of what SUNI can reach, one level at a time.

It is drawn behind the head, so it has to be cheap and it has to be honest: a
level that quietly drops half a folder is worse than one that says how much it
left out, and a picture is never worth failing a request over.

Nothing here touches the filesystem — folders and files come from the document
index, so the graph cannot enumerate a disk SUNI was never pointed at.
"""
from __future__ import annotations

import os

import pytest

from suni import graph


class FakeStore:
    """Just enough document store: the graph reads _meta and the two counts."""

    def __init__(self, paths):
        self._meta = {i: {"file_path": p} for i, p in enumerate(paths)}

    def count(self):
        return len(self._meta)

    def file_count(self):
        return len({m["file_path"] for m in self._meta.values()})


class FakeRegistry:
    def __init__(self, names):
        self._names = names

    def names(self):
        return list(self._names)


def _paths(*rel):
    base = os.path.join("D:" + os.sep, "Work")
    return [os.path.join(base, *r.split("/")) for r in rel]


@pytest.fixture(autouse=True)
def _fresh_cache():
    graph._tree_cache.update({"key": None, "dirs": {}, "files": {}})
    yield


# ── the root level ───────────────────────────────────────────────────────────
def test_the_root_names_the_things_suni_can_reach():
    g = graph.build("root", registry=FakeRegistry(["a", "b"]),
                    doc_store=FakeStore(_paths("x/one.pdf")))
    ids = {n["id"] for n in g["nodes"]}
    assert {"machine", "models", "tools", "skills", "channels", "kb"} <= ids
    assert g["trail"][0]["label"] == "SUNI"


def test_every_node_is_connected_to_the_focus():
    g = graph.build("root", doc_store=FakeStore(_paths("x/one.pdf")))
    assert {e["to"] for e in g["edges"]} == {n["id"] for n in g["nodes"]}
    assert all(e["from"] == "root" for e in g["edges"])


def test_no_index_means_no_knowledge_node():
    g = graph.build("root", doc_store=FakeStore([]))
    assert "kb" not in {n["id"] for n in g["nodes"]}


# ── the indexed tree ─────────────────────────────────────────────────────────
def test_a_lone_root_is_stepped_through_rather_than_shown():
    """A drive, then a company, then "Projects" is three clicks that say nothing."""
    g = graph.build("kb", doc_store=FakeStore(_paths("a/one.pdf", "b/two.pdf")))
    labels = {n["label"] for n in g["nodes"]}
    assert labels == {"a", "b"}, "the walk down to the first real choice did not happen"
    assert [t["label"] for t in g["trail"]][:2] == ["SUNI", "knowledge"]


def test_a_folder_says_how_much_is_underneath_it():
    # Two top folders, so the view stops here instead of stepping into one.
    store = FakeStore(_paths("a/one.pdf", "b/deep/two.pdf", "b/deep/three.pdf"))
    g = graph.build("kb", doc_store=store)
    b = next(n for n in g["nodes"] if n["label"] == "b")
    assert b["count"] == 2, "the count stops at the first level instead of rolling up"


def test_drilling_in_shows_folders_and_files_together():
    store = FakeStore(_paths("a/one.pdf", "a/deep/two.pdf", "b/other.pdf"))
    kb = graph.build("kb", doc_store=store)
    a = next(n for n in kb["nodes"] if n["label"] == "a")
    g = graph.build(a["id"], doc_store=store)
    kinds = {n["kind"] for n in g["nodes"]}
    assert kinds == {"folder", "file"}
    f = next(n for n in g["nodes"] if n["kind"] == "file")
    assert f["label"] == "one.pdf" and f["ext"] == "pdf"


def test_the_trail_leads_back_out():
    store = FakeStore(_paths("a/deep/two.pdf"))
    g = graph.build(f"dir:{os.path.join('D:' + os.sep, 'Work', 'a', 'deep')}",
                    doc_store=store)
    labels = [t["label"] for t in g["trail"]]
    assert labels[0] == "SUNI" and labels[-1] == "deep"
    assert "a" in labels, "the level above is not reachable from here"


def test_a_crowded_folder_says_what_it_left_out(monkeypatch):
    monkeypatch.setattr(graph, "MAX_CHILDREN", 5)
    store = FakeStore(_paths(*[f"a/f{i}.pdf" for i in range(12)], "b/other.pdf"))
    kb = graph.build("kb", doc_store=store)
    a = next(n for n in kb["nodes"] if n["label"] == "a")
    g = graph.build(a["id"], doc_store=store)
    assert len(g["nodes"]) == 6, "the cap did not hold"
    last = g["nodes"][-1]
    assert last["more"] == 7 and "+7" in last["label"], \
        "seven files vanished without the view admitting it"


# ── the other levels ─────────────────────────────────────────────────────────
def test_tools_come_from_the_live_registry():
    g = graph.build("tools", registry=FakeRegistry(["send_email", "web_search"]))
    assert {n["label"] for n in g["nodes"]} == {"send_email", "web_search"}


def test_channels_report_whether_they_are_configured():
    g = graph.build("channels", config={"telegram_bot_token": "x"})
    by = {n["label"]: n for n in g["nodes"]}
    assert by["telegram"].get("live") == 1
    assert by["slack"].get("live", 0) == 0


def test_models_list_the_chain_without_repeating_the_primary():
    g = graph.build("models", config={
        "model": "qwen2.5:7b",
        "model_chain": [{"model": "qwen2.5:7b", "enabled": True},
                        {"model": "gpt-oss:120b", "enabled": False}]})
    labels = [n["label"] for n in g["nodes"]]
    assert labels.count("qwen2.5:7b") == 1
    assert "gpt-oss:120b" in labels


# ── it must not fail the page ────────────────────────────────────────────────
def test_a_broken_store_yields_an_empty_map_not_an_error():
    class Broken:
        _meta = {}

        def count(self):
            raise RuntimeError("index unreadable")

        def file_count(self):
            raise RuntimeError("index unreadable")

    g = graph.build("kb", doc_store=Broken())
    assert g["nodes"] == []


def test_an_unknown_focus_is_empty_rather_than_an_error():
    g = graph.build("nonsense:whatever")
    assert g["nodes"] == [] and g["focus"] == "nonsense:whatever"


def test_labels_are_trimmed_so_one_long_name_cannot_fill_the_view():
    g = graph.build("tools", registry=FakeRegistry(["x" * 200]))
    assert len(g["nodes"][0]["label"]) <= graph._LABEL_MAX


def test_only_indexed_files_can_be_opened():
    """The allow-list for opening is the index itself, not the disk."""
    store = FakeStore(_paths("a/one.pdf"))
    allowed = graph.indexed_paths(store)
    assert allowed == set(_paths("a/one.pdf"))
    assert os.path.join("C:" + os.sep, "Windows", "system32") not in allowed
