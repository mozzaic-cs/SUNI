"""The network the Face draws behind her head.

Ambient it is grey and slow and asks for nothing. The moment the viewer takes
hold of it — drag, wheel, click — it takes her colour and the head steps aside,
the same corner move the content stage makes.

These are static checks over the page and the module, which is what the rest of
this suite does for interface work: the drawing itself is judged by looking at
it, but the wiring can rot silently, and every one of these has a specific way
of going wrong.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FACE = (ROOT / "suni/web/face.html").read_text(encoding="utf-8")
MOD = (ROOT / "suni/web/netgraph.js").read_text(encoding="utf-8")
SERVER = (ROOT / "suni/web/server.py").read_text(encoding="utf-8-sig")


def test_the_module_is_loaded_and_served():
    assert '<script src="/netgraph.js">' in FACE
    assert '@app.get("/netgraph.js")' in SERVER, "the page would load a 404"


def test_the_field_is_built_on_the_faces_own_context():
    """A second WebGL context for the same canvas would fail; it has to share."""
    assert "new NetGraph(gl)" in FACE


def test_it_is_drawn_behind_the_head_not_over_it():
    i = FACE.index("drawBackground();")
    block = FACE[i:i + 700]
    assert "_net.draw(" in block, "the field is not drawn with the background"
    # Depth writes are what put it behind her, rather than draw order alone.
    assert "depthMask(false)" in MOD and "DEPTH_TEST" in MOD


def test_ambient_is_grey_and_focus_takes_her_colour():
    assert "stateColor(stateSmooth)" in FACE[FACE.index("_net.draw("):FACE.index("_net.draw(") + 200] \
        or "nc[0] / 255" in FACE
    assert "mix(grey, u_tint, u_focus)" in MOD, "focus does not tint the field"


def test_taking_hold_of_it_moves_the_head_aside_without_opening_the_panel():
    """The stage panel is 60% of the screen with a backdrop: opening it would
    cover the very thing the head moved aside for."""
    i = FACE.index("_stageT +=")
    line = FACE[i:i + 160]
    assert "_netFocus" in line, "focus does not move the head"
    setfocus = FACE[FACE.index("function _netSetFocus"):][:400]
    assert "_setStage(" not in setfocus, "focus opens the content panel over the network"


@pytest.mark.parametrize("gesture", ["pointerdown", "pointermove", "pointerup", "wheel"])
def test_every_gesture_is_wired(gesture):
    assert f"canvas.addEventListener('{gesture}'" in FACE


def test_a_drag_orbits_and_a_click_selects():
    move = FACE[FACE.index("canvas.addEventListener('pointermove'"):][:900]
    assert ".orbit(" in move
    up = FACE[FACE.index("canvas.addEventListener('pointerup'"):][:900]
    assert ".pick(" in up, "clicking cannot select a node"
    assert "_netMoved" in up, "a drag that ends over a node would count as a click"


def test_escape_leaves_the_network_alone_again():
    assert "Escape" in FACE and "_netSetFocus(false)" in FACE


def test_only_openable_things_drill_in():
    up = FACE[FACE.index("canvas.addEventListener('pointerup'"):][:1200]
    assert "startsWith('dir:')" in up, "clicking any node would try to open it"
    assert "_netLoad(id)" in up


def test_the_trail_is_clickable_so_a_viewer_can_get_back_out():
    assert "_netTrail.addEventListener('click'" in FACE
    assert "data-id" in FACE


def test_labels_are_escaped():
    """Folder names come off a disk and land in innerHTML."""
    assert "function _esc(" in FACE
    describe = FACE[FACE.index("function _netDescribe"):][:900]
    assert "_esc(node.label)" in describe


def test_the_popup_follows_its_node_every_frame():
    """The field drifts; a label pinned where the node was is worse than none."""
    assert "_netPaint()" in FACE
    paint = FACE[FACE.index("function _netPaint"):][:600]
    assert "screenPos(" in paint


def test_the_view_fits_whatever_level_it_is_given():
    assert "distWant = Math.max(" in MOD, "six nodes and a hundred need the same room"


def test_a_shader_failure_leaves_the_page_working():
    """WebGL compile failures are a fact of life across drivers; the Face must
    still be a face."""
    assert "this.ok = !!(this.prog && this.lprog)" in MOD
    assert "if (!this.ok) return;" in MOD
    assert "_net && _net.ok" in FACE
