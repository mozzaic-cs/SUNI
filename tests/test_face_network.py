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


def test_both_moods_are_in_colour_and_focus_is_the_brighter_one():
    """Ambient was greyscale first. Behind her head the categories still have to
    be tellable apart, so it keeps its colours and only drops in brightness."""
    assert "nc[0] / 255" in FACE, "her state colour never reaches the field"
    assert "vec3 cat = mix(v_color, u_tint" in MOD, "nodes ignore their category colour"
    # The brightness factor is tuned by eye, so the test asks what it has to be
    # true of rather than what it currently reads: dimmer than focus in ambient,
    # and focus adds rather than subtracts. A pinned literal here failed on a
    # tuning pass that had not broken anything.
    factors = re.findall(r"\(([\d.]+) \+ ([\d.]+) \* u_focus\)", MOD)
    assert factors, "node brightness does not depend on focus at all"
    for base, gain in factors:
        assert float(gain) > 0, "focus dims the field instead of brightening it"
        assert float(base) < 1.0, "ambient is already at full brightness"

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
    # Anchored on the brace: "function _netPaint" is a prefix of
    # "function _netPaintLabels", so the loose form windows the wrong function.
    paint = FACE[FACE.index("function _netPaint(){"):][:900]
    assert "screenPos(" in paint


def test_the_view_fits_whatever_level_it_is_given():
    assert "distWant = Math.max(" in MOD, "six nodes and a hundred need the same room"


def test_a_shader_failure_leaves_the_page_working():
    """WebGL compile failures are a fact of life across drivers; the Face must
    still be a face."""
    assert "this.ok = !!(this.prog && this.lprog)" in MOD
    assert "if (!this.ok) return;" in MOD
    assert "_net && _net.ok" in FACE


# ── names ────────────────────────────────────────────────────────────────────
def test_names_appear_as_nodes_grow_on_screen():
    """Zooming in makes points bigger, so keying labels off drawn size is the
    whole rule: far out only the hubs are named, close in everything is."""
    assert "labels(w, h, opts)" in MOD
    assert "minPx" in MOD and "px < minPx" in MOD
    assert "minPx: _netFocus ? 14 : 26" in FACE, "ambient names as many as focus does"


def test_names_never_land_on_her_face():
    assert "exclude: _headBox(w, h)" in FACE, "nothing tells the labels where she is"
    assert "function _headBox(" in FACE
    box = FACE[FACE.index("function _headBox("):][:700]
    assert "_stageT" in box, "the box does not follow her to the corner"
    assert "o.exclude" in MOD and "skip.x0" in MOD


def test_overlapping_names_are_dropped_not_nudged():
    """A label moved away from its node points at nothing."""
    # The whole method, not a fixed window of it: a comment added at the top
    # pushed the line this was looking for off the end of an 1800-character
    # slice, and the test failed over prose.
    start = MOD.index("labels(w, h, opts)")
    block = MOD[start:MOD.index(chr(10) + "    }", start)]
    assert "spacing" in block and "clear = false" in block


def test_labels_reuse_a_pool_of_elements():
    """Creating and destroying spans at sixty hertz is how a smooth canvas gets
    a stuttering overlay."""
    assert "_netLabelPool" in FACE
    assert "document.createElement('span')" in FACE


def test_ambient_keeps_its_colours():
    """Grey was the first instinct and the wrong one: the categories stay
    readable behind her head, just quieter."""
    i = MOD.index("vec3 cat = mix(v_color")
    block = MOD[i - 400:i + 400]
    assert "vec3 grey" not in block, "ambient still drains the colour out"


def test_the_atlas_cell_is_worked_out_before_the_fragment_stage():
    """The star flickered and no other icon did.

    "skill" is the icon whose index lands exactly on a row boundary, and
    mod()/floor() on it at mediump is a coin toss: 4.0/4.0 comes back as
    0.99999 and the glyph drops a whole row into an empty cell. Vertex-stage
    float is highp, and a varying carrying small whole numbers survives.
    """
    assert "varying vec2  v_cell;" in MOD, "the cell is not passed from the vertex stage"
    vs = MOD[MOD.index("const VS ="):MOD.index("const FS =")]
    assert "v_cell = vec2(" in vs, "the cell is still worked out per fragment"
    fs = MOD[MOD.index("const FS ="):]
    assert "floor(v_icon" not in fs, "the fragment stage still divides the icon index"
    assert "GL_FRAGMENT_PRECISION_HIGH" in MOD, "no highp where the device has it"


def test_there_is_a_visible_way_back_to_her():
    """Escape worked from the first version, which is no use to anyone who does
    not already know it is there. A head shrunk into a corner with no way back
    is a trap, not a mode."""
    assert 'id="net-exit"' in FACE, "nothing on screen leads back"
    assert "_netExitBtn.addEventListener('click', _netExit)" in FACE
    exit_fn = FACE[FACE.index("function _netExit()"):][:500]
    assert "_netSetFocus(false)" in exit_fn, "the head never comes back to full size"
    assert "spotlight(null)" in exit_fn, "a category stays singled out after leaving"
    assert "setCluster(false)" in exit_fn, "the groups stay pulled apart after leaving"
    # Escape and the button are the same door.
    esc = FACE[FACE.index("e.key === 'Escape'"):][:120]
    assert "_netExit()" in esc


def test_clicking_a_category_takes_you_to_it():
    assert 'data-kind="${item.kind}"' in FACE, "legend rows do not say which category"
    assert "_netSpot(row.dataset.kind)" in FACE, "legend rows do not answer to a click"
    spot = MOD[MOD.index("spotlight(kind) {"):][:900]
    assert "lookWant" in spot, "the camera never travels to the category"
    # A spotlight, not a filter: the rest stays drawn, dark and in place, so it
    # is visible that they are still there.
    shade = MOD[MOD.index("_uploadShade() {"):][:700]
    assert "this.spot" in shade and "* 0.22" in shade, (
        "the other categories are hidden rather than dimmed")
    assert "this.spot = null" in MOD, "loading a level leaves a stale category dimmed"


def test_the_camera_can_look_at_something_other_than_the_origin():
    """Travelling to a group is meaningless if the camera only ever orbits the
    middle."""
    view = MOD[MOD.index("    view() {"):][:320]
    assert "this.look" in view, "the view matrix ignores where the camera is looking"
    assert "60" in MOD[MOD.index("    zoom(delta)"):][:200], (
        "the zoom clamp is tighter than the distance the cluster view asks for, "
        "so the first wheel notch snaps it back")


def test_the_centre_node_is_named_too():
    """It is the thing everything on screen hangs off - her at the top level,
    whatever you opened below it - and it was the one node with no name."""
    lab = MOD[MOD.index("labels(w, h, opts)"):MOD.index(chr(10) + "    }",
                                                        MOD.index("labels(w, h, opts)"))]
    assert "this.center.label" in lab, "the centre is still an anonymous dot"
    assert "label: (graph && graph.trail" in MOD, "the centre never learns its name"
