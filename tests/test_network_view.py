"""SUNI pointing at the field, and the field doing something when told.

Two halves, with different failure modes:

  Her half must never become discovery. The view has refused to scan anything
  since it was built, and a tool that "shows the network" is exactly where that
  would quietly change - the temptation being to make the demo look better when
  neighbour discovery is switched off. It must say the setting is off instead.

  The viewer's half opens files. The document index is system-wide and single,
  so being in it is NOT a per-user permission; treating it as one would have
  let the most restricted account read anything that had ever been scanned.
"""
from __future__ import annotations

import ast
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL_SRC = (ROOT / "suni/tools/network_view_tool.py").read_text(encoding="utf-8")
SERVER = (ROOT / "suni/web/server.py").read_text(encoding="utf-8-sig")
FACE = (ROOT / "suni/web/face.html").read_text(encoding="utf-8")
APPROVAL = (ROOT / "suni/approval.py").read_text(encoding="utf-8")


# ── her half ────────────────────────────────────────────────────────────────
def test_showing_the_network_never_probes_it():
    """The same rule the view itself has kept: this reads what is already
    known. Docstrings are dropped first - the last time this was checked, the
    test passed on the word "pings" in the prose that promised not to."""
    # Names, not text. Twice now a scan test has passed or failed on prose:
    # first on the word "pings" in a docstring promising not to, and then on
    # the sentence that tells the user nothing is scanned either way. What is
    # being checked is what the code CALLS and IMPORTS, so look at identifiers.
    tree = ast.parse(TOOL_SRC)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            names.add(node.attr.lower())
        elif isinstance(node, ast.Import):
            names.update(a.name.split(".")[0].lower() for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0].lower())
    for probe in ("socket", "subprocess", "asyncio", "httpx", "requests",
                  "popen", "connect", "ping", "scan"):
        assert probe not in names, f"the view tool reaches out to the network: {probe}"


def test_an_empty_network_says_which_kind_of_empty_it_is():
    """A view with nothing in it because a setting is off looks exactly like a
    view with nothing in it because there is nothing there. Telling somebody
    with seventeen machines on their LAN the second one is the bad failure."""
    assert "network_neighbours" in TOOL_SRC
    i = TOOL_SRC.index("network_neighbours")
    assert "admin panel" in TOOL_SRC[i - 600:i + 600], (
        "it reports the emptiness without naming the setting behind it")


def test_changing_the_view_is_not_an_action_that_needs_approving():
    """An approval card in front of every "show me the network" is a feature
    nobody uses twice."""
    assert '"show_network"' not in APPROVAL, "a view change asks for approval"


def test_the_reply_stands_up_without_a_face():
    """Telegram, a schedule and the API have no picture to move. The text is
    the answer; the view is an accompaniment."""
    assert "No Face open" in TOOL_SRC
    emit = TOOL_SRC[TOOL_SRC.index("def _emit"):TOOL_SRC.index("def handler")]
    assert "return False" in emit, "a missing UI is treated as an error"


def test_it_is_registered_where_the_face_can_reach_it():
    """The server keeps its own registry; a tool registered only in main.py is
    a tool the web interface has never heard of."""
    assert "network_view_tool.SCHEMA" in SERVER
    assert "network_view_tool.set_sources(" in SERVER, (
        "without the stores every level comes back empty, which looks exactly "
        "like a machine with nothing on it")


def test_the_face_answers_when_she_moves_the_view():
    assert "ev.type === 'net_view'" in FACE
    block = FACE[FACE.index("ev.type === 'net_view'"):][:1200]
    assert "[NET_NAV]" in block, "no marker to tell 'did not work' from 'did not load'"
    assert "_netSpot(ev.spot, true)" in block, (
        "told twice to show the machines she would hide them the second time")
    assert "_netData.focus" in block, (
        "the category is lit without checking which level actually landed")


# ── the viewer's half ───────────────────────────────────────────────────────
def test_opening_a_file_is_gated_on_the_role_not_only_the_index():
    """The index is system-wide and single. "It is in the index" is not a
    per-user permission, and gating on it alone hands every indexed file to the
    most restricted account on the machine."""
    i = SERVER.index('@app.get("/api/graph/open")')
    block = SERVER[i:i + 4000]
    assert "search_knowledge_base" in block, "no role ceiling on opening a file"
    assert "_rbac.allowed_tools" in block and "_rbac.blocked_tools" in block
    assert "is_indexed" in block, "any path on the disk could be opened"
    assert "_audit.log" in block, "a file read through a picture is still a file read"
    assert "_BLOCKED_DL_EXTS" in block


@pytest.fixture(autouse=True)
def _fresh_tree_cache():
    """The tree is cached on (chunk count, file count), which identifies the
    index size and not the index. One real store per process makes that fine in
    production and wrong across two fake stores of the same size in a test."""
    from suni import graph
    graph._tree_cache.update({"key": None, "indexed": None, "indexed_key": None})
    yield
    graph._tree_cache.update({"key": None, "indexed": None, "indexed_key": None})


def test_being_under_an_indexed_folder_is_not_being_indexed(tmp_path):
    """The index holds the files it was told to read. Everything the scanner
    skipped is also "under an indexed folder"."""
    from suni import graph

    indexed = tmp_path / "docs" / "real.txt"
    indexed.parent.mkdir()
    indexed.write_text("x", encoding="utf-8")
    skipped = tmp_path / "docs" / "skipped.bin"
    skipped.write_text("x", encoding="utf-8")

    class _Store:
        _meta = {"1": {"file_path": str(indexed)}}

        def count(self): return 1
        def file_count(self): return 1

    store = _Store()
    assert graph.is_indexed(store, str(indexed))
    assert not graph.is_indexed(store, str(skipped))
    assert not graph.is_indexed(store, "")


def test_dots_cannot_walk_out_of_the_allow_list(tmp_path):
    """Compared on the resolved real path, or the allow-list is a suggestion."""
    from suni import graph

    inside = tmp_path / "docs" / "real.txt"
    inside.parent.mkdir()
    inside.write_text("x", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("x", encoding="utf-8")

    class _Store:
        _meta = {"1": {"file_path": str(inside)}}

        def count(self): return 1
        def file_count(self): return 1

    store = _Store()
    # The same file by a longer road is still the same file...
    assert graph.is_indexed(store, str(tmp_path / "docs" / ".." / "docs" / "real.txt"))
    # ...and a road out of it does not become one.
    assert not graph.is_indexed(store, str(tmp_path / "docs" / ".." / "secret.txt"))


def test_a_node_offers_what_makes_sense_for_its_kind():
    acts = FACE[FACE.index("function _netActions"):][:2200]
    assert "Open it" in acts and "Copy the path" in acts
    # A machine on the LAN offers nothing that touches it: reading the ARP
    # cache is as close as this view has ever gone.
    machine = acts[acts.index("m.kind === 'machine'"):]
    assert "_netOpenFile" not in machine, "the menu can reach out to a neighbour"


def test_a_click_on_a_folder_still_descends():
    """It has meant that since the first version; the menu is an addition, not
    a replacement."""
    up = FACE[FACE.index("canvas.addEventListener('pointerup'"):][:1400]
    assert "_netLoad(id)" in up
    assert "contextmenu" in FACE, "there is no way to reach a folder's actions"


@pytest.mark.parametrize("view", ["machines", "models", "files", "overview"])
def test_every_advertised_view_maps_to_a_real_level(view):
    from suni.graph import KINDS  # noqa: F401 — import proves the module loads
    from suni.tools import network_view_tool as nv

    level, _what = nv.VIEWS[view]
    assert level in {"overview", "network", "models", "tools", "skills",
                     "channels", "agents", "kb"}
    assert view in nv.SCHEMA["parameters"]["properties"]["view"]["enum"]


def test_the_orchestrator_builder_does_not_reach_for_stores_it_cannot_see():
    """This one is written from a live outage.

    The tool's stores were handed over inside _build_orchestrator, which is
    called before the document store exists and never receives it. The result
    was a NameError at import: the server did not start at all, and the Face -
    served from disk, so already updated - offered a button for an endpoint
    that was not there. Nothing in the suite noticed, because every test that
    would have imported the app was running against the same broken file.

    So: the builder gets a parameter or it does not get the value.
    """
    tree = ast.parse(SERVER)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_build_orchestrator")
    params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assigned = {t.id for node in ast.walk(fn) for t in ast.walk(node)
                if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store)}
    imported = {(a.asname or a.name).split(".")[0]
                for node in ast.walk(fn)
                if isinstance(node, (ast.Import, ast.ImportFrom)) for a in node.names}
    known = params | assigned | imported
    for name in ("doc_store", "memory_store", "app"):
        used = any(isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)
                   for n in ast.walk(fn))
        assert not (used and name not in known), (
            f"_build_orchestrator reads {name}, which is not a parameter, "
            f"not assigned there, and not imported: that is a NameError at "
            f"import and a server that does not start")


# ── recognising the ask without the model ───────────────────────────────────
def _view_for(text):
    from suni.core.orchestrator import _NETVIEW_ASK_RE, _NETVIEW_TARGETS
    ask = _NETVIEW_ASK_RE.search(text)
    if not ask:
        return None
    for name, pattern in _NETVIEW_TARGETS:
        hit = pattern.search(text)
        if hit and ask.start() < hit.start():
            return name
    return None


@pytest.mark.parametrize("text,view", [
    ("Quais os dispositivos na rede?", "machines"),
    ("Mostra-me todos os equipamentos da rede", "machines"),
    ("mostra-me a rede", "machines"),
    ("Show all devices on the network", "machines"),
    ("mostra os modelos que tens", "models"),
    ("Que ferramentas tens disponiveis?", "tools"),
    ("quais os canais configurados?", "channels"),
    ("show me the agents", "agents"),
    ("Mostra-me as pastas indexadas", "files"),
    ("list the indexed files", "files"),
])
def test_asking_to_see_something_moves_the_view(text, view):
    """The model was asked to call show_network and did not - [ROUTE] local, no
    [TOOL] line, the answer in words and the picture never moving. Same as
    schedules and named agents, where a 7B failed tool selection 4 times out of
    4. If the intent can be recognised without the model, recognise it."""
    assert _view_for(text) == view


@pytest.mark.parametrize("text", [
    "a rede esta muito lenta hoje",              # a statement about the network
    "manda um email aos agentes comerciais",     # agents, but not to look at
    "mostra-me a fatura do mes passado",         # an ask, but not for this
    "o modelo que compraste ontem",              # "que" as a relative pronoun
    "apaga os agentes que nao uso",              # an instruction, not a question
    "os ficheiros que me mostraste ontem",       # the ask comes after the thing
    "preciso de ferramentas para o jardim",
    "que horas sao?",
])
def test_it_does_not_fire_on_everything_that_mentions_a_noun(text):
    """Both halves alone are false-positive machines: "mostra" matches an
    invoice, "rede" matches a complaint about wifi. Portuguese "que" is a
    relative pronoun as often as an interrogative, which is why the ask has to
    come before the thing asked for."""
    assert _view_for(text) is None


@pytest.mark.parametrize("view", ["overview", "machines", "models", "tools",
                                  "skills", "channels", "agents", "files"])
def test_every_view_actually_runs(view):
    """Written after a live failure that every other test in this file missed.

    `from ..config import config` is wrong - suni.config is a module of
    functions, not an object - so the handler raised ImportError on its first
    line of real work and returned "Could not read the machines view". The
    module still imported, still parsed, and every check here was a string
    search over source that could not possibly have noticed.

    So: call it. No mocks, no fixtures, no stores - the point is to execute the
    imports and the formatting on the real thing.
    """
    from suni.tools import network_view_tool as nv

    out = nv.handler(view=view)
    assert isinstance(out, str) and out
    assert "Could not read" not in out, out[:200]
    assert "Traceback" not in out


# ── naming an agent, in both languages ──────────────────────────────────────
@pytest.mark.parametrize("text,slug", [
    ("usa o agente Revisor para ler isto", "revisor"),
    ("Pede ao agente Revisor um resumo", "revisor"),
    ("ask the Reviewer agent to check this", "reviewer"),
    ("use agent Reviewer on this file", "reviewer"),
])
def test_an_agent_can_be_named_in_either_language(text, slug):
    """English puts the noun last, Portuguese puts it first and spells it
    differently. Only the English order was matched, so naming an agent in
    pt-PT fell through to an ordinary turn and SUNI answered as herself with
    no sign the agent had been asked for."""
    from suni.core.schedule_intent import parse
    known = [{"slug": "revisor", "name": "Revisor"},
             {"slug": "reviewer", "name": "Reviewer"}]
    assert parse(text, known)["agent_slug"] == slug


@pytest.mark.parametrize("text", [
    "o agente da policia passou aqui",     # the common noun, not a name
    "preciso de um agente de viagens",
    "os agentes comerciais ligaram",
    "manda isto para revisao",
])
def test_the_common_noun_is_not_an_agent_name(text):
    """A capital letter is what separates the two. "o agente da policia" must
    not resolve to an agent called "Da"."""
    from suni.core.schedule_intent import parse
    known = [{"slug": "revisor", "name": "Revisor"}]
    assert not parse(text, known)["agent_slug"]


# ── the models view tells the truth about what answers ──────────────────────
def _models(cfg):
    from suni import graph
    return graph.build("models", config=cfg)["nodes"]


def test_a_tier_with_no_model_string_is_still_a_model():
    """The Claude Code tier carries no model name - the CLI chooses its own -
    and the old code required one, so the tier that answered most turns was the
    one thing missing from "which models do you have"."""
    cfg = {"model": "qwen2.5:7b", "model_chain": [
        {"id": "tier-cc", "label": "Claude Code (T5)", "provider": "claude-code",
         "model": "", "enabled": True}]}
    labels = [n["label"] for n in _models(cfg)]
    assert any("Claude Code" in x for x in labels), labels


def test_an_empty_switched_off_tier_is_a_slot_not_a_model():
    """Listing "Large (T3)" as something she can think with is worse than
    listing nothing: it is an answer that is not true."""
    cfg = {"model": "qwen2.5:7b", "model_chain": [
        {"id": "tier-large", "label": "Large (T3)", "provider": "ollama",
         "model": "", "enabled": False}]}
    labels = [n["label"] for n in _models(cfg)]
    assert not any("Large" in x for x in labels), labels


def test_the_embedding_and_image_models_are_models_too():
    cfg = {"model": "m", "embed_model": "nomic-embed-text",
           "image_gen_enabled": True, "image_gen_model": "runwayml/stable-diffusion-v1-5"}
    nodes = _models(cfg)
    labels = [n["label"] for n in nodes]
    assert "nomic-embed-text" in labels
    assert "stable-diffusion-v1-5" in labels, labels
    detail = {n["label"]: n.get("detail", "") for n in nodes}
    assert "embeddings" in detail["nomic-embed-text"]


def test_a_remote_openai_compatible_model_appears_without_being_in_the_chain():
    """It is configured on its own, so a chain-only view never showed it."""
    cfg = {"model": "m", "vllm_model": "gpt-4o-mini",
           "vllm_base_url": "https://api.example.com/v1", "backend": "vllm"}
    nodes = _models(cfg)
    hit = [n for n in nodes if n["label"] == "gpt-4o-mini"]
    assert hit, [n["label"] for n in nodes]
    # The host is named but never its credentials.
    assert "api.example.com" in hit[0].get("detail", "")
    assert "@" not in hit[0].get("detail", "")
