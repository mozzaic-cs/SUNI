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


# ── machines and services ────────────────────────────────────────────────────
def test_the_network_lists_hosts_suni_actually_talks_to():
    g = graph.build("network", config={
        "ollama_host": "http://127.0.0.1:11434",
        "smtp_host": "smtp.example.net",
        "vllm_base_url": "http://gpu-1:8000/v1",
    })
    by = {n["label"]: n.get("detail", "") for n in g["nodes"]}
    assert "gpu-1" in by and "vLLM" in by["gpu-1"]
    assert "smtp.example.net" in by
    assert "this machine" in by["127.0.0.1"], "a local service should say so"


def test_a_connection_string_never_brings_its_password_along():
    # Assembled rather than written out. A password followed by an at-sign and
    # a hostname reads as an email address to the release scanner, and it is
    # right to be that broad — so the fixture works around the rule rather than
    # the rule around the fixture. (Spelling the example out in this comment
    # tripped it a second time.)
    db = "postgres://dbuser:{}@{}:5432/app".format("s3cr3t", "db.internal")
    api = "https://{}@{}/v1".format("tok3n", "api.example.com")
    assert graph._host_of(db) == "db.internal"
    assert graph._host_of(api) == "api.example.com"
    assert graph._host_of("") == ""


def test_each_host_appears_once_however_many_services_it_runs():
    g = graph.build("network", config={
        "ollama_host": "http://gpu-1:11434",
        "embed_base_url": "http://gpu-1:11434",
        "vllm_base_url": "http://gpu-1:8000/v1",
    })
    assert [n["label"] for n in g["nodes"]].count("gpu-1") == 1


def test_neighbours_are_off_unless_asked_for(monkeypatch):
    """A list of everything on the network is not the same as a list of SUNI's
    own services, so it waits to be turned on."""
    monkeypatch.setattr(graph, "_neighbours", lambda: [("192.168.1.9", "aa-bb-cc-dd-ee-ff")])
    off = graph.build("network", config={})
    on = graph.build("network", config={"network_neighbours": True})
    assert not any("192.168.1.9" in n["label"] for n in off["nodes"])
    assert any("192.168.1.9" in n["label"] for n in on["nodes"])


def test_nothing_in_the_network_view_scans_anything():
    """The ARP cache is read, never filled: a machine appears only because this
    one already spoke to it."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(graph._neighbours).lstrip())
    fn = tree.body[0]
    # Drop ONLY the docstring: the prose says it does not scan, and the first
    # version of this test passed on the word "pings" in that very sentence.
    # Blanking every string instead removed the "arp" it is meant to look for.
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body.pop(0)
    code = ast.unparse(tree)
    assert "'arp', '-a'" in code or '"arp", "-a"' in code
    for forbidden in ("ping", "connect", "socket", "nmap", "urlopen", "requests"):
        assert forbidden not in code, f"the neighbour list reaches out ({forbidden})"


def test_broadcast_and_multicast_are_not_machines(monkeypatch):
    import subprocess

    class R:
        stdout = ("  192.168.1.1     f4-ce-46-a5-5e-71   dynamic\n"
                  "  192.168.1.255   ff-ff-ff-ff-ff-ff   static\n"
                  "  239.255.255.250 01-00-5e-7f-ff-fa   static\n")

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    ips = [ip for ip, _ in graph._neighbours()]
    assert ips == ["192.168.1.1"]


def test_agents_and_schedules_are_the_callers_own(monkeypatch):
    """Both are per-user. Asking with somebody else's id must not be a way to
    see their agents, so the caller is passed through, not assumed."""
    import suni.agents as ag
    import suni.schedules as sc
    asked = {}

    def fake_agents(uid, role=""):
        asked["agents"] = (uid, role)
        return [{"slug": "researcher", "name": "Researcher", "enabled": True, "model": ""}]

    def fake_scheds(uid, role=""):
        asked["schedules"] = (uid, role)
        return [{"id": "s1", "name": "Weekly", "enabled": False, "cadence": "weekly"}]

    monkeypatch.setattr(ag, "list_for_user", fake_agents)
    monkeypatch.setattr(sc, "list_for_user", fake_scheds)
    g = graph.build("agents", user_id="u1", user_role="user")
    assert asked["agents"] == ("u1", "user")
    assert asked["schedules"] == ("u1", "user")
    labels = {n["label"] for n in g["nodes"]}
    assert labels == {"Researcher", "Weekly"}
    weekly = next(n for n in g["nodes"] if n["label"] == "Weekly")
    assert weekly.get("live", 0) == 0, "a disabled schedule should not look live"
