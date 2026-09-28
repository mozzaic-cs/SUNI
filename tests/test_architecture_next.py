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
