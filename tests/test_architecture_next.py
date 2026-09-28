"""The architecture page as panels in 3D.

The flat page is 81 boxes and 4,000 words at one depth. This prototype carries
the same prose behind a camera: subsystems, their parts, then the full text.

What these check is the handful of decisions that make it work at all — not
how it looks, which is judged by looking at it.
"""
from __future__ import annotations

import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE = (ROOT / "suni/web/architecture_next.html").read_text(encoding="utf-8")
MOD = (ROOT / "suni/web/archgraph.js").read_text(encoding="utf-8")
SERVER = (ROOT / "suni/web/server.py").read_text(encoding="utf-8-sig")
DOC = json.loads((ROOT / "suni/web/architecture.json").read_text(encoding="utf-8"))


def test_the_prototype_does_not_touch_the_page_it_is_replacing():
    """A redesign nobody can compare against the original is a redesign nobody
    can judge. The flat page keeps its URL and keeps working."""
    assert '@app.get("/architecture")' in SERVER
    assert '@app.get("/architecture/next")' in SERVER
    assert 'href="/architecture"' in PAGE, "no way back to the page it replaces"


def test_every_route_the_page_needs_exists():
    for route in ('"/architecture/next"', '"/archgraph.js"', '"/architecture.json"'):
        assert f"@app.get({route})" in SERVER, f"{route} would 404"


def test_the_content_is_behind_the_same_auth_as_the_page():
    """Component names alone say what this machine is built to do."""
    i = SERVER.index('@app.get("/architecture.json")')
    assert "_check_page_auth" in SERVER[i:i + 700], "the data is public while the page is not"


def test_the_text_is_html_not_textures():
    """The whole reason it is built this way. Four thousand words rendered into
    WebGL would be blurry, unsearchable, unselectable and unreadable to a
    screen reader — and the prose is the value of this page."""
    assert "createTexture" not in MOD and "WebGLRenderingContext" not in MOD
    assert "getContext('webgl" not in PAGE
    assert "position:absolute" in PAGE and "translate(-50%,-50%)" in PAGE
    # Panels are placed by projecting a 3D point, which is what makes it 3D.
    assert "project(pos, w, h)" in MOD


def test_panels_never_shrink_below_readable():
    """A panel scaled to a third has 5px type in it. That is not a distant
    panel, it is a smudge that used to be words."""
    i = MOD.index("scale: Math.max(")
    line = MOD[i:i + 120]
    floor = float(line.split("Math.max(")[1].split(",")[0])
    assert floor >= 0.45, f"panels can shrink to {floor} of their size"


def test_a_sequence_stays_a_sequence():
    """The orchestrator lane is nine numbered steps, not nine peers. Laid out
    as a cloud it loses the only thing it was saying."""
    seq = [g for g in DOC["groups"] if g.get("kind") == "sequence"]
    assert seq, "no ordered group survived the conversion"
    lay = MOD[MOD.index("_relayout()"):MOD.index("openGroup(id)")]
    assert "kind === 'sequence'" in lay, "order is not laid out differently"
    for g in seq:
        titles = [i["title"] for i in g["items"]]
        assert titles == sorted(titles, key=lambda t: g["items"][titles.index(t)]["where"]), (
            "the steps are not in their original order")


@pytest.mark.parametrize("edge", DOC["edges"], ids=lambda e: f"{e['from']}->{e['to']}")
def test_every_edge_connects_two_things_that_exist(edge):
    """An edge is a claim. One pointing at a group that is not there draws
    nothing and says nothing, and nothing errors to tell you."""
    ids = {g["id"] for g in DOC["groups"]}
    assert edge["from"] in ids, f"edge from unknown group {edge['from']!r}"
    assert edge["to"] in ids, f"edge to unknown group {edge['to']!r}"
    assert edge.get("note"), "an edge with no note is a line, not a relationship"


def test_the_prose_came_across_rather_than_being_summarised():
    """It was carried over from the hand-written page verbatim: retyping 4,000
    words would only introduce errors, and the detail is the point."""
    items = [it for g in DOC["groups"] for it in g["items"]]
    assert len(items) >= 30, f"only {len(items)} components made it across"
    assert all(it["title"] and it["lead"] for it in items), "a component with no summary"
    words = sum(len(" ".join([it["lead"]] + it["body"]).split()) for it in items)
    assert words > 1500, f"only {words} words survived — this is a summary, not the page"


def test_a_phase_chip_never_ends_up_inside_a_title():
    """The flat page baked "Phase 13" and "new" into the title markup. Carried
    across literally, every other panel would be titled "… Phase 13"."""
    for g in DOC["groups"]:
        for it in g["items"]:
            assert "Phase " not in it["title"], f"{it['title']!r} carries its badge"
            assert not it["title"].endswith("new"), f"{it['title']!r} carries its badge"


# ── the whole page came across, not a sample ────────────────────────────────
def test_every_lane_of_the_flat_page_is_here():
    """The first pass converted four subsystems by hand-picking them. The flat
    page has thirteen, and three of its lanes use markup the first converter
    did not know about — it silently produced zero items for them."""
    flat = (ROOT / "suni/web/architecture.html").read_text(encoding="utf-8")
    assert len(DOC["groups"]) == 13, f"{len(DOC['groups'])} subsystems, not 13"
    items = sum(len(g["items"]) for g in DOC["groups"])
    # The flat page's own count of component headings.
    assert items == flat.count("node-title"), (
        f"{items} components carried across, {flat.count('node-title')} on the page")
    for g in DOC["groups"]:
        assert g["items"], f"{g['id']} came across empty"
        assert g["lead"], f"{g['id']} has no summary line"


def test_the_flat_inventories_survived():
    """Some lanes list bare names — tools, components — and a list of names is
    a fact about breadth. Thirty more panels would have buried it, and dropping
    it would have lost it."""
    named = sum(len(g.get("inventory", [])) for g in DOC["groups"])
    assert named >= 25, f"only {named} inventory names carried across"


@pytest.mark.parametrize("group", DOC["groups"], ids=lambda g: g["id"])
def test_every_tone_is_one_the_renderer_knows(group):
    """An unknown tone silently falls back to cyan, so two subsystems quietly
    become the same colour and the index stops meaning anything."""
    tones = MOD[MOD.index("const TONE = {"):MOD.index("class ArchGraph")]
    assert f"{group['tone']}:" in tones, f"{group['tone']!r} is not a defined tone"


# ── layout has one hard constraint ──────────────────────────────────────────
def test_layouts_are_spaced_by_the_real_width_of_a_panel():
    """A panel is the same width in WORLD units at every distance: it is drawn
    at 18/d of its CSS size while a world unit spans a height/d of pixels, and
    the two cancel. So a layout spaced more tightly than a panel overlaps at
    every zoom, and pulling the camera back never fixes it — which is exactly
    what the first sequence layout did."""
    assert "PANEL_W" in MOD, "the constraint is not written down anywhere"
    lay = MOD[MOD.index("_relayout()"):MOD.index("openGroup(id)")]
    assert lay.count("PANEL_W") >= 2, "a layout is still spaced by a guessed number"


def test_panels_are_projected_into_the_room_the_chrome_leaves():
    """The index down the left covered three subsystems, which simply never
    appeared for anyone who did not think to drag."""
    assert "this.inset" in MOD
    assert "function applyInset()" in PAGE
    proj = MOD[MOD.index("project(pos, w, h)"):][:700]
    assert "this.inset.left" in proj and "this.inset.bottom" in proj


# ── finding things ──────────────────────────────────────────────────────────
def test_you_can_search_every_word_not_only_what_is_on_screen():
    """"Is it in here at all" is the question people actually have, and it
    cannot be answered by looking at the panels currently drawn."""
    assert 'id="q"' in PAGE
    blk = PAGE[PAGE.index("qEl.addEventListener('input'"):][:900]
    assert "for (const g of DOC.groups)" in blk, "search only looks at what is drawn"
    assert "textOf(it)" in blk


def test_there_is_a_list_of_everything_as_well_as_a_picture():
    """A field you have to orbit to discover is a field you can miss things in.
    The index says what exists without asking anyone to fly around."""
    assert "function drawIndex()" in PAGE
    idx = PAGE[PAGE.index("function drawIndex()"):][:700]
    assert "DOC.groups" in idx and "items || []).length" in idx


def test_the_reader_can_be_paged_through():
    """Reading one component and then hunting for the next one in a 3D field
    is not reading."""
    for hook in ("read-prev", "read-next", "function step(d)"):
        assert hook in PAGE
    assert "ArrowLeft" in PAGE and "ArrowRight" in PAGE


def test_a_first_visit_is_told_how_this_works():
    """Three depths and a camera is not a convention anyone has seen before."""
    assert 'id="intro"' in PAGE
    assert "arch_intro" in PAGE, "the tour has no way to stay dismissed"
    assert "tour" in PAGE, "no way to skip it when linking somebody in"
