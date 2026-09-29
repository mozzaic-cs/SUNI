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
    # To the end of the function, not a fixed window of it: this asserted over
    # 900 characters and failed when a block was added at the top, which is the
    # second time a slice-sized test has broken over an insertion.
    start = FACE.index("function _netPaint(){")
    paint = FACE[start:FACE.index(chr(10) + "}", start)]
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


def test_clusters_are_laid_out_facing_the_viewer():
    """Groups on a sphere put half of themselves edge-on or behind the middle,
    and at the distance needed to fit that sphere they are small, scattered and
    crossed by every spoke — a picture of a scatter, not of groups. On a disc
    every group is the same distance from the eye and all of them are in frame.
    """
    lay = MOD[MOD.index("_clusterLayout() {"):MOD.index("setCluster(on) {")]
    assert "2.399963" in lay, "groups are not spread by golden angle on a disc"
    assert "centre[2] + p[2] * rad * 0.45" in lay, "groups are balls, not discs"
    # One group per category. Splitting a category into bands is for a level
    # where everything is one kind; two clusters of tools is not "by category".
    assert "const PER = 40" in lay, "ordinary categories get split in half"


def test_the_view_is_framed_from_what_the_layout_actually_spans():
    """A single extent understates the vertical reach of a wide disc, and the
    group that falls off the bottom edge is the one being looked for."""
    assert "_extentY" in MOD and "_extentX" in MOD
    # The maths lives in _fitDistance() now, because five layouts need it and
    # only one of them is the clustering that first asked for it.
    fit = MOD[MOD.index("_fitDistance() {"):][:600]
    assert "needY" in fit and "needX" in fit, "one axis frames both"
    # Sliced to the end of each method rather than a fixed count: a comment
    # inside setCluster pushed the call past a 900-character window and failed
    # the test over prose. That has happened four times in this suite now.
    for caller in ("setCluster(on) {", "setLayout(name) {"):
        i = MOD.index(caller)
        blk = MOD[i:MOD.index(chr(10) + "    }", i)]
        assert "_fitDistance()" in blk, f"{caller} does not reframe the view"


def test_a_roomier_layout_is_not_a_roomier_picture():
    """The camera pulls back to fit whatever the layout spans, so spacing the
    groups further apart just moves the camera back and shrinks every node
    below the size that can carry an icon or a name."""
    lay = MOD[MOD.index("_clusterLayout() {"):MOD.index("setCluster(on) {")]
    import re as _re
    m = _re.search(r"radOf = \(n\) => ([\d.]+) \+ Math\.sqrt\(n\) \* ([\d.]+)", lay)
    assert m, "the group radius is no longer a simple function of size"
    assert float(m.group(2)) < 0.4, "groups grow faster than the camera can follow"


def test_the_middle_of_the_field_goes_up_a_level():
    """The centre is drawn from index 0 of the buffers and has no entry in
    this.nodes, so pick() can never return it and a click there did nothing —
    on the most obvious target on the screen, which also happens to BE the
    level you are on."""
    assert "pickCentre(x, y, w, h)" in MOD, "the centre still cannot be clicked"
    assert "function _netUp()" in FACE
    up = FACE[FACE.index("function _netUp()"):][:420]
    # The trail is the authority on what "up" means: the server builds it and
    # already knows a chain of single-child folders counts as one step.
    assert "_netData.trail" in up, "up is guessed from the path instead of the trail"
    up_hit = FACE[FACE.index("canvas.addEventListener('pointerup'"):][:1600]
    assert "pickCentre" in up_hit, "clicking the middle does nothing"
    assert up_hit.index("_net.pick(") < up_hit.index("pickCentre"), (
        "the centre is tested before the nodes, so a node in front of it "
        "would never be clickable")


def test_grouping_has_a_control_the_keyboard_cannot_swallow():
    """It was a keyboard shortcut, which was useless: the chat box takes focus
    on load and again after every reply, so the key handler — which correctly
    ignores keys typed into an input — never saw it. Pressing C typed a c into
    the message box, and the mode had most likely never once been on.

    The text button that replaced it has itself been replaced, by the row of
    layout icons: "group by category" said exactly what the "areas" icon beside
    it said, and two controls for one state is how they end up disagreeing. The
    guarantee is unchanged — a click target a text field cannot swallow, which
    shows whether the mode is on.
    """
    assert 'id="net-lay"' in FACE, "grouping is keyboard-only again"
    assert "_netLay.querySelectorAll('button').forEach" in FACE, "the icons do nothing"
    # The page builds its buttons from NetGraph.layouts(), so the arrangement
    # is named in the MODULE — asserting it against the page looked right and
    # checked nothing about either.
    assert "id: 'areas'" in MOD, "the grouping arrangement is not offered"
    assert "NetGraph.layouts()" in FACE, "the page does not build from the registry"
    # One place changes the mode, so the icons, the key and SUNI cannot
    # disagree about which state it is in.
    assert "function _netCluster(" in FACE
    for caller in ("_netCluster(!_net.clustered)", "_netCluster(true)"):
        assert caller in FACE, f"{caller} bypasses the single door"
    assert FACE.count("_net.setCluster(") == 2, (
        "a third direct setCluster call: the controls can now go stale")
    # A visible state is what tells "it did not work" from "it never fired".
    assert "[NET_CLUSTER]" in FACE and "_netMarkLayout()" in FACE


def test_an_icon_only_control_still_says_what_it_does():
    """An icon without words is a rebus. The name AND the reason both appear,
    because "rings" alone does not say what rings are for — and the tooltip
    opens to the LEFT, since this column sits against the right edge of the
    window where one growing rightwards would be cut off."""
    assert "data-tip=" in FACE, "the icons have no tooltip"
    assert "aria-label=" in FACE, "the icons are unreadable to a screen reader"
    css = FACE[FACE.index("#net-lay button::after"):][:400]
    assert "content:attr(data-tip)" in css
    assert "right:calc(100% + " in css, "the tooltip opens off the edge of the window"


def test_back_remembers_where_you_were_not_where_the_level_sits():
    """The trail says where a level SITS: a drive's parent is the knowledge
    base, whatever route you arrived by. From the overview everything is one
    click away, so opening the drive and then climbing the trail lands on a
    level the viewer has never seen."""
    assert "const _netHist = []" in FACE
    back = FACE[FACE.index("function _netBack()"):][:400]
    assert "_netHist.pop()" in back
    assert "_netUp()" in back, "running out of history should still go somewhere"
    load = FACE[FACE.index("async function _netLoad"):][:1200]
    assert "o.back" in load, "going back pushes the place you just left"
    # Pushed from the level on screen, so a failed load cannot record a place
    # the viewer was never at.
    assert "_netData.focus !== focus" in load


def test_every_control_that_hides_itself_is_shown_by_the_focus_switch():
    """Written because two of them were not, and I said they were.

    The pattern in this page is: base rule hides the element (opacity 0,
    pointer-events none), a class of "on" reveals it. An element that follows
    that pattern and is never given the class is invisible forever, and nothing
    errors — the button is in the DOM, the handler is bound, the click can just
    never happen. The exit pill was toggled nowhere at all; the group pill was
    toggled only when a level loaded, which is not when focus changes.

    So this finds them in the stylesheet rather than listing them, and any new
    one is caught the day it is added.
    """
    focus_fn = FACE[FACE.index("function _netSetFocus"):]
    focus_fn = focus_fn[:focus_fn.index(chr(10) + "}")]

    # Driven by something other than focus, with the reason it is exempt.
    exempt = {
        "net-pop": "follows the pointer: shown on hover, hidden on leave",
        "net-menu": "opened by a click on a node, closed by the next one",
        "net-labels": "a pool of spans, each turned on per frame by position",
    }

    hidden = re.findall(r"#(net-[\w-]+)\{([^}]*)\}", FACE)
    hidden = {el: body for el, body in hidden if "opacity:0" in body}
    # Any non-zero opacity counts as "revealed": the legend uses .92, so a rule
    # looking only for opacity:1 skipped it entirely.
    revealed = dict(re.findall(r"#(net-[\w-]+)\.on\{([^}]*)\}", FACE))
    for el in sorted(set(hidden) & set(revealed)):
        if el in exempt:
            continue
        # Each element's OWN variable, read out of its getElementById line
        # rather than guessed from the id — the first version of this test
        # matched any _net*.classList.toggle in the function, so one real
        # toggle made every element pass and the test proved nothing.
        m = re.search(rf"(?:const|let|var)\s+(\w+)\s*=\s*document\.getElementById\("
                      rf"'{re.escape(el)}'\)", FACE)
        assert m, f"#{el} has no element variable to check"
        var = m.group(1)
        assert f"{var}.classList.toggle('on', on)" in focus_fn, (
            f"#{el} hides itself and is never revealed by _netSetFocus, so it "
            f"is a control nobody can see or click")
        # Opacity is not the only thing a hidden control switches off. The
        # legend was visible, hover-styled, cursor:pointer, bound to a handler
        # and unclickable, because .on restored the opacity and not the
        # pointer-events its base rule had disabled.
        if "pointer-events:none" in hidden[el]:
            assert "pointer-events:auto" in revealed[el], (
                f"#{el} becomes visible but stays untouchable: its base rule "
                f"disables pointer-events and .on never restores them")


# ── five ways of looking at the same nodes ──────────────────────────────────
@pytest.mark.parametrize("layout", ["orbit", "rings", "circle", "areas", "force"])
def test_every_offered_layout_exists(layout):
    """The page builds its buttons from NetGraph.layouts(), so a name offered
    there and not implemented is a button that silently does nothing."""
    assert f"'{layout}'" in MOD[MOD.index("static layouts()"):][:700], (
        f"{layout} is not offered")
    if layout != "orbit":
        fn = {"areas": "_clusterLayout", "rings": "_ringsLayout",
              "circle": "_circleLayout", "force": "_forceLayout"}[layout]
        assert f"{fn}()" in MOD, f"{layout} is offered but {fn} does not exist"


def test_switching_layout_is_a_move_not_a_new_screen():
    """Each layout writes only `target`; update() walks the nodes there. That
    is the whole reason to have more than one — watching a category gather
    itself out of the cloud is a different understanding from seeing it already
    gathered."""
    for fn in ("_ringsLayout", "_circleLayout", "_forceLayout"):
        block = MOD[MOD.index(fn + "() {"):][:2600]
        assert "nd.target" in block or "nd.target =" in block, (
            f"{fn} does not move nodes, it must be setting positions directly")
        assert ".pos =" not in block, f"{fn} teleports nodes instead of moving them"


def test_a_new_level_arrives_in_the_chosen_layout():
    """Opening a folder used to throw the arrangement away and drop back to
    orbit, which reads as the view resetting itself for no reason."""
    sd = MOD[MOD.index("setData(graph) {"):][:2600]
    assert "_applyLayout()" in sd, "a new level ignores the chosen layout"


def test_the_force_layout_settles():
    """A fixed number of iterations, run once. A field that never stops moving
    is a field you cannot read, and one that jiggles for ever spends the frame
    budget on nothing."""
    block = MOD[MOD.index("_forceLayout() {"):][:2600]
    assert "for (let iter = 0; iter <" in block, "the force layout has no bound"
    assert "requestAnimationFrame" not in block, "it runs every frame"


# ── the hologram, and what it must not cost ─────────────────────────────────
def test_the_glow_is_added_not_blended():
    """Light adds; surfaces do not. Drawn in one pass the halo composited at
    low alpha against a near-black background and vanished, and what little
    showed punched a depth hole around every node."""
    assert "u_pass" in MOD, "there is no separate pass for the light"
    d = MOD[MOD.index("gl.uniform1f(this.u.pass, 1.0)"):][:400]
    assert "gl.blendFunc(gl.SRC_ALPHA, gl.ONE)" in d, "the glow is not additive"
    assert "gl.depthMask(false)" in d, "each halo carves a hole out of the next"
    assert "gl.depthMask(true)" in d, "depth writes are left off for the next frame"


def test_there_is_no_framebuffer_pass():
    """A bloom target at this machine's resolution is tens of megabytes on an
    8 GB card that is ALREADY evicting the language model to make room — that
    was measured, not assumed. The whole hologram is per-fragment arithmetic."""
    for forbidden in ("createFramebuffer", "bindFramebuffer", "createRenderbuffer"):
        assert forbidden not in MOD, f"{forbidden} costs VRAM this box does not have"


def test_the_field_borrows_the_heads_own_light():
    """So the two read as one object rather than as a chart in front of a
    hologram: the same cool fresnel rim, scanlines and flicker as FS2."""
    fs = MOD[MOD.index("const FS ="):MOD.index("const LVS =")]
    assert "rimCol" in fs, "no fresnel rim"
    assert "gl_FragCoord.y * 1.5" in fs, "no scanlines, and at the head's own pitch"
    assert "u_time" in fs, "no flicker"
