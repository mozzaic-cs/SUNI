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
GRAPH = (ROOT / "suni/graph.py").read_text(encoding="utf-8")


def test_the_module_is_loaded_and_served():
    assert '<script src="/netgraph.js">' in FACE
    assert '@app.get("/netgraph.js")' in SERVER, "the page would load a 404"


def test_the_field_is_built_on_the_faces_own_context():
    """A second WebGL context for the same canvas would fail; it has to share."""
    assert "new NetGraph(gl)" in FACE


def test_it_is_drawn_behind_the_head_not_over_it():
    # To the end of the block, not a fixed 700 characters of it: a comment
    # added inside pushed the call past the window, which is the third time a
    # slice-sized test in this file has failed over prose rather than code.
    i = FACE.index("drawBackground();")
    block = FACE[i:FACE.index("_netPaint();", i)]
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
    up = FACE[FACE.index("canvas.addEventListener('pointerup'"):][:1600]
    assert "_netOpens(hit.meta)" in up, "clicking any node would try to open it"
    assert "_netLoad(id)" in up


def test_what_opens_is_asked_of_the_node_not_a_list_in_the_page():
    """The page kept its own list of openable ids and the list fell behind the
    server. "agents" became a level and the list never heard about it, so the
    one hub with the most inside it said nothing on hover and did nothing on
    click. The node carries the answer now."""
    assert "function _netOpens(" in FACE
    assert "meta.opens" in FACE, "the page ignores what the server said"
    assert "opens=1" in GRAPH, "the server never says which nodes open"
    # Every level the server actually serves should be reachable. This is the
    # check the old hardcoded list could not make.
    served = set(re.findall(r'focus == "(\w+)"', GRAPH))
    for level in served - {"root", "overview", "kb"}:
        assert f'_node("{level}"' in GRAPH, (
            f'the "{level}" level exists but nothing on the root opens it'
        )


def test_the_remainder_of_a_level_is_worth_clicking():
    """It used to arrive as kind "folder" with nothing handling the click: it
    looked like something to open and did nothing when opened, which is worse
    than not showing it at all."""
    assert '"more"' in GRAPH, "the remainder still wears another kind's clothes"
    assert "next=nxt" in GRAPH, "the remainder does not say what to ask for next"
    up = FACE[FACE.index("canvas.addEventListener('pointerup'"):][:1600]
    assert "startsWith('more:')" in up, "clicking the remainder does nothing"
    assert "limit: Number(hit.meta.next)" in up, (
        "the page invents its own page size instead of using the server's"
    )
    assert "back: true" in up, (
        "asking for more pushes onto the back stack, so Back walks through "
        "every size the viewer ever asked for"
    )


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


def test_names_never_land_on_her_face_or_in_the_dock():
    """Two zones, not one. A node just above the dock printed its name
    straight through the AI-disclosure line — caught on a real screen, not
    in the harness, because the harness has no dock."""
    made = re.search(r"const keepClear = \[([^\]]*)\]", FACE)
    assert made, "the labels are no longer told what to keep clear"
    assert "_headBox(w, h)" in made.group(1), "nothing tells the labels where she is"
    assert "_dockBox(w, h)" in made.group(1), "the dock is not kept clear"
    assert "exclude: keepClear" in FACE, "the list is built and then not used"
    box = FACE[FACE.index("function _headBox("):][:700]
    assert "_stageT" in box, "the box does not follow her to the corner"
    dock = FACE[FACE.index("function _dockBox("):][:900]
    assert "getBoundingClientRect" in dock, (
        "the dock box is guessed, so it is wrong the moment the dock resizes"
    )
    assert "o.exclude" in MOD and "skips.some" in MOD

def test_a_name_is_tested_where_it_is_actually_drawn():
    """The name sits below its node. Testing only the node let a label fall
    into a zone the node itself had cleared, and keeping the offset in two
    places is how they drift apart."""
    assert "function labelDrop(px)" in MOD, "the offset has no single home"
    assert MOD.count("px * 0.55 + 4") == 1, "the offset is written down twice"
    assert "blocked(p.x, ty)" in MOD, "only the node is tested, not the name"
    assert "item.ty" in FACE, "the page places the span with its own copy of the offset"


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
    # Escape and the button are the same door. Anchored on the network's OWN
    # handler, not on the first "Escape" in the file: a second panel added its
    # own Escape listener higher up and this windowed that one instead.
    esc = FACE[FACE.index("e.key === 'Escape' && _netFocus"):][:120]
    assert "_netExit()" in esc


def test_escape_closes_one_thing_at_a_time():
    """The mode help and the network both answer Escape. One press should shut
    the thing in front of you, not that and the thing behind it."""
    i = FACE.index("e.key === 'Escape' && _netFocus")
    guard = FACE[max(0, i - 260):i]
    assert "_modeHelpIsOpen()" in guard or "_suniHelpClosed" in guard, (
        "leaving the help open also drops out of the network"
    )
    # Both orders, because which listener runs first is registration order.
    assert "e._suniHelpClosed = true" in FACE, (
        "if the help's listener runs first, the network's still fires"
    )


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


# ── the nebula: colour behind the clusters ──────────────────────────────────


def test_the_nebula_is_drawn_first_of_all():
    """A wash painted after the nodes is a wash painted over them."""
    body = MOD[MOD.index("    draw(proj, tint, canvasH) {"):]
    body = body[:body.index("\n    }\n")]
    assert "this._drawNebula()" in body, "the wash is never drawn"
    assert body.index("this._drawNebula()") < body.index("gl.useProgram(this.lprog)"), (
        "the wash is painted after the links, so it covers them"
    )


def test_the_wash_fades_with_the_field():
    """Behind her head it must be a hint; with the floor to itself, a presence.

    A constant gain was the first thing tried and it fought her face.
    """
    call = re.search(r"gl\.uniform1f\(this\.nu\.gain,([^)]*)\)", MOD)
    assert call, "the gain is no longer set"
    assert "this.focus" in call.group(1), "the wash no longer follows the focus"


def test_the_wash_is_not_a_framebuffer():
    """An 8 GB card that is already evicting the model cannot spare a render
    target. If this ever grows one, it is a deliberate decision, not a drift."""
    assert "createFramebuffer" not in MOD


def test_every_category_fits_in_the_uniform_array():
    """More categories than slots and the last ones silently lose their wash."""
    cap = int(re.search(r"const NEB_MAX = (\d+)", MOD).group(1))
    kinds = len(re.findall(r"^\s{4}(\w+):\s*\{ color:", MOD, re.M))
    assert kinds <= cap, f"{kinds} categories will not fit in {cap} slots"


def test_a_smeared_category_earns_no_wash():
    """The first render of the rings layout went white.

    Every kind is spread evenly round the circle there, so every centroid is
    the middle of the screen, and ten washes stacked on one point saturate.
    The wash has to be weighted by how concentrated the kind really is.
    """
    body = MOD[MOD.index("_clusters(w, h) {"):]
    body = body[:body.index("\n    }\n")]
    assert "spread / (h *" in body, "the wash no longer measures concentration"
    wash = MOD[MOD.index("_drawNebula() {"):]
    assert re.search(r"col\.push\(c\.col\[0\] \* c\.tight", wash), (
        "the concentration weight is computed but never reaches the colour"
    )


def test_one_node_is_not_a_cluster():
    """A lone node has a spread of zero, which the concentration measure
    reads as perfect — a single drive lit up harder than the twenty files
    inside it."""
    body = MOD[MOD.index("_clusters(w, h) {"):]
    body = body[:body.index(chr(10) + "    }" + chr(10))]
    assert re.search(r"tight \*=.*\bn\b", body), (
        "the wash no longer accounts for how many nodes are in the category"
    )

def test_the_wash_is_quieter_behind_her_head_than_the_nodes():
    """Ambient is her face's state, not the field's. At 0.16 the wash was the
    brightest thing on an ambient screen, louder than the nodes it is meant to
    sit behind."""
    call = re.search(r"gl\.uniform1f\(this\.nu\.gain, ([\d.]+) \+ ([\d.]+)", MOD)
    assert call, "the gain is no longer a base plus a focused share"
    base, lift = float(call.group(1)), float(call.group(2))
    assert base <= 0.08, f"ambient gain {base} will read over the nodes"
    assert lift >= 3 * base, "the wash barely changes between the two states"


def test_the_wash_cannot_reach_white():
    """Whatever overlaps, the background must stay a background."""
    assert "sum = sum / (1.0 + sum);" in MOD


# ── hubs: what a whole cluster IS ───────────────────────────────────────────


def test_a_cluster_says_what_it_is():
    """A field of coloured dots says how much of each thing there is. Only
    the marker says WHAT they are, which is the question a viewer has
    first — and it is the one thing the reference renders had that ours
    did not."""
    assert "hubs(w, h, opts)" in MOD, "the module offers no cluster markers"
    assert "_netPaintHubs" in FACE, "the page never draws them"
    assert 'id="net-hubs"' in FACE, "there is no layer to draw them into"


def test_a_marker_is_a_louder_claim_than_a_wash():
    """A smear that earns a faint haze earns no name: in an arrangement
    where the kinds are interleaved there is no "here" to point at, and
    saying nothing is more honest than a word in the middle of
    everything."""
    body = MOD[MOD.index("hubs(w, h, opts) {"):]
    body = body[:body.index(chr(10) + "    }" + chr(10))]
    bar = re.search(r"o\.min \|\| ([\d.]+)", body)
    assert bar, "the marker has no concentration bar at all"
    assert float(bar.group(1)) > 0.02, (
        "the bar is no higher than the wash, so every smear gets a name"
    )
    assert "c.n >= 2" in body, "a single node would be labelled as a cluster"


def test_a_marker_sits_beside_its_cluster_not_on_it():
    """Placed at the centroid it lands on the very nodes it describes. The
    reference puts each one at the outer edge, which is what makes a screen
    of six hundred dots readable."""
    body = MOD[MOD.index("hubs(w, h, opts) {"):]
    body = body[:body.index(chr(10) + "    }" + chr(10))]
    assert "c.spread" in body, "the offset ignores how big the cluster is"
    assert "x: c.x + ux * off" in body, "the marker is still at the centroid"


def test_markers_are_placed_before_names_so_names_give_way():
    """A cluster's name outranks any one node's. Painted the other way,
    a marker arrives on top of a label already placed there."""
    start = FACE.index("function _netPaintLabels(){")
    paint = FACE[start:FACE.index(chr(10) + "}", start)]
    assert paint.index("_netPaintHubs(") < paint.index("_net.labels("), (
        "names are placed before the markers they are supposed to dodge"
    )
    assert "keepClear.concat(hubs)" in paint, "the names never hear about the markers"


def test_a_long_name_is_tested_at_both_ends():
    """The span is centred on its node, so a long name reaches well either
    side of the point being tested. "stable-diffusion" ran into a marker
    whose box its own centre had cleared comfortably."""
    assert "function textHalfWidth(" in MOD, "nothing measures the text"
    assert "blocked(p.x - half, ty)" in MOD and "blocked(p.x + half, ty)" in MOD, (
        "only the middle of the name is tested"
    )


def test_markers_stay_out_of_the_ambient_view():
    """Ambient is her face's state. A row of captions across it is exactly
    the clutter the ambient view exists to avoid."""
    start = FACE.index("function _netPaintHubs(")
    body = FACE[start:FACE.index(chr(10) + "}", start)]
    assert "if (!_netFocus)" in body, "the markers show behind her head too"



def test_the_icon_atlas_is_a_power_of_two():
    """Not a style rule — a hard WebGL 1 requirement, and it fails LOUDLY in
    the worst way: the atlas is mipmapped with LINEAR_MIPMAP_LINEAR, and
    generateMipmap on a non-power-of-two texture leaves it incomplete, so it
    samples BLACK. Growing the grid from 4x128 to 5x128 made 640, and the
    result was not three wrong icons — it was every icon on the screen
    vanishing at once.
    """
    cols = int(re.search(r"ATLAS_COLS = (\d+)", MOD).group(1))
    cell = int(re.search(r"ATLAS_CELL = (\d+)", MOD).group(1))
    side = cols * cell
    assert side & (side - 1) == 0, (
        f"the atlas is {side}px a side, which is not a power of two: "
        "generateMipmap will fail and every icon will draw black"
    )


def test_every_glyph_has_a_cell_to_live_in():
    """One past the end does not fail, it WRAPS — a node quietly wears another
    kind's icon, which is the sort of bug that survives a long time."""
    order = re.search(r"const ICON_ORDER = \[(.*?)\];", MOD, re.S).group(1)
    glyphs = re.findall(r'"(\w+)"', order)
    cols = int(re.search(r"ATLAS_COLS = (\d+)", MOD).group(1))
    assert len(glyphs) <= cols * cols, (
        f"{len(glyphs)} glyphs will not fit in {cols}x{cols} cells"
    )
    # And every kind's icon has to BE in that list, or it wears cell zero.
    for icon in re.findall(r'icon: "(\w+)"', MOD):
        assert icon in glyphs, f'kind icon "{icon}" is not in ICON_ORDER'


# ── ring guides ─────────────────────────────────────────────────────────────


def test_the_rings_layout_actually_draws_rings():
    """It put one category on each orbit from the day it was written and never
    drew the orbits, so it read as scattered dots that happened to curve."""
    assert "_drawRings()" in MOD, "the guides are never drawn"
    assert "RING_SEGMENTS" in MOD
    body = MOD[MOD.index("_ringsLayout() {"):]
    body = body[:body.index("\n    }\n")]
    assert "this.rings.push(" in body, (
        "the layout does not record where its rings are, so the guides would "
        "have to work it out a second time"
    )


def test_guides_do_not_outlive_their_layout():
    """Circles drawn through an arrangement that has no rings in it."""
    for layout in ("_circleLayout", "_forceLayout", "_clusterLayout"):
        body = MOD[MOD.index(f"{layout}() {{"):][:400]
        assert "this.rings = []" in body, f"{layout} leaves the guides up"
    apply = MOD[MOD.index("_applyLayout() {"):]
    apply = apply[:apply.index("\n    }\n")]
    assert "this.rings = []" in apply, "leaving for orbit leaves the guides up"


def test_a_ring_says_which_category_it_is():
    assert "ringLabels(w, h, opts)" in MOD, "the rings are anonymous circles"
    assert "_netPaintRingNames" in FACE, "the page never writes them"
    assert "_netSpot(el._kind)" in FACE, "a ring's name does not go to its category"


def test_a_category_is_not_named_twice():
    """A cluster marker saying MODEL beside a ring labelled MODEL is the same
    word twice for the same thing."""
    body = MOD[MOD.index("hubs(w, h, opts) {"):]
    body = body[:body.index("\n    }\n")]
    assert "if (this.rings.length) return []" in body, (
        "cluster markers and ring names would both label every category"
    )


# ── the floor ───────────────────────────────────────────────────────────────


def test_there_is_a_floor_and_it_is_under_everything():
    """Everything on this page floated in nothing."""
    assert "_drawFloor(tint)" in MOD, "the floor is never drawn"
    body = MOD[MOD.index("    draw(proj, tint, canvasH) {"):]
    body = body[:body.index("\n    }\n")]
    first = body.index("this._drawFloor(")
    for later in ("this._drawNebula()", "this._drawRings()", "gl.useProgram(this.lprog)"):
        assert first < body.index(later), f"the floor is painted after {later}"


def test_the_floor_is_screen_space_on_purpose():
    """The head and the field are drawn through two different cameras — the
    head has its own mvp and, once it shrinks, its own viewport in the corner.
    A plane placed in either one lines up with that one and drifts from the
    other the moment she moves."""
    body = MOD[MOD.index("_drawFloor(tint) {"):]
    body = body[:body.index("\n    }\n")]
    assert "this._mvp" not in body and "uniformMatrix4fv" not in body, (
        "the floor has been put into one of the two camera spaces"
    )
    assert "gl.disable(gl.DEPTH_TEST)" in body


def test_the_floor_turns_with_the_field():
    """A floor that stays put while the network turns above it reads as a
    photograph of a floor."""
    body = MOD[MOD.index("_drawFloor(tint) {"):]
    body = body[:body.index("\n    }\n")]
    assert "this.yaw" in body, "the grid never pans"


def test_the_projector_sits_under_her_and_travels_with_her():
    assert "_net.emitter = 0.5 + 0.37 * _stageT" in FACE, (
        "the light comes from a fixed point while she moves away from it"
    )
    # Same journey the head box makes, so the two agree about where she is.
    box = FACE[FACE.index("function _headBox("):][:700]
    assert "w * 0.87" in box


def test_the_floor_follows_focus_like_everything_else():
    body = MOD[MOD.index("_drawFloor(tint) {"):]
    body = body[:body.index("\n    }\n")]
    call = re.search(r"this\.fu\.gain, ([\d.]+) \+ ([\d.]+) \* this\.focus", body)
    assert call, "the floor does not fade with the field"
    base, lift = float(call.group(1)), float(call.group(2))
    # This asked for base <= 0.25 when it was written, which was taste dressed
    # up as a requirement: at 0.15 the floor was invisible behind her full-size
    # head, and it was asked for back. What actually has to hold is that
    # ambient is the QUIETER of the two states, not that it is dark.
    assert lift > 0, "the floor does not brighten when the field takes the screen"
    assert base < base + lift <= 1.0, "ambient is as bright as focus, or over it"


def test_the_floor_costs_no_memory():
    """Another light source, on a card that is already evicting the model."""
    body = MOD[MOD.index("_drawFloor(tint) {"):]
    body = body[:body.index("\n    }\n")]
    assert "this.bQuad" in body, "the floor allocated its own buffer"
    assert "createFramebuffer" not in MOD


def test_the_way_back_to_her_is_an_icon_like_its_neighbours():
    """It was a pill reading "back to SUNI" in a column of icons — the one
    control that said its name out loud, and the only one that could not be
    translated, because the words were baked into the markup."""
    assert '<button id="net-exit" type="button"></button>' in FACE, (
        "the exit control still carries its label in the HTML"
    )
    assert "function _netBuildExit(" in FACE
    assert "iconDataURL('face'" in FACE, "it has no face on it"
    assert "t('face.net_exit')" in FACE, "the label is not translated"
    # One set of rules for the whole column, so the two cannot drift apart.
    assert "#net-lay button, #net-exit{" in FACE


def test_the_field_no_longer_writes_messages_into_its_own_exit_button():
    """_netToast used to replace the button's contents and put them back after
    a couple of seconds, because that button was the only text on the field.
    Overloading the one control that gets somebody out is a poor place for a
    warning, and there is a real notice now."""
    fn = FACE[FACE.index("function _netToast(msg){"):]
    fn = fn[:fn.index("\n}")]
    assert "_showToast(msg, 'warn')" in fn
    assert "_netExitBtn.innerHTML" not in fn, (
        "the warning still overwrites the way out"
    )


# ── what she is doing ───────────────────────────────────────────────────────


def test_the_pool_carries_her_state():
    """The label was 7.5px at 42% alpha in a corner — smaller than any other
    text on the page, for the fact a person most wants at a glance. The pool
    is the brightest thing on screen and already under her, so it carries the
    state in motion as well."""
    assert "uniform float u_state;" in MOD
    body = MOD[MOD.index("_drawFloor(tint) {"):]
    body = body[:body.index("\n    }\n")]
    assert "this.fu.state" in body and "this.fu.level" in body
    assert "_net.state = stateSmooth" in FACE, "the page never tells it"
    assert "_net.level = mouthOpen" in FACE, (
        "speaking flares on a timer rather than on her actual voice"
    )


def test_the_states_crossfade_rather_than_switch():
    """stateSmooth is eased, and the weights are a distance, so two states can
    be partly true at once and the pool moves between them."""
    assert "float w(float s)" in MOD
    assert "1.0 - abs(u_state - s)" in MOD


def test_a_nan_can_never_reach_the_floor_shader():
    """This one cost an hour. The harness passed NaN for the voice level, and
    a NaN does not misdraw one thing: it is multiplied by a state weight that
    is usually ZERO, and 0 * NaN is NaN — so it poisoned the sum and then
    every colour added after it, and the ENTIRE floor disappeared, grid
    included. The shader compiled and linked perfectly throughout, which is
    why neither of those checks noticed."""
    body = MOD[MOD.index("_drawFloor(tint) {"):]
    body = body[:body.index("\n    }\n")]
    assert "Number(this.state) || 0" in body, "a non-number still reaches u_state"
    assert "Number(this.level) || 0" in body, "a non-number still reaches u_level"


def test_the_state_word_is_readable():
    """7.5px at 42% alpha, in the corner, for the one fact that matters most."""
    css = FACE[FACE.index("#state-label{"):]
    css = css[:css.index("}")]
    size = float(re.search(r"font-size:([\d.]+)px", css).group(1))
    assert size >= 12, f"{size}px is no better than it was"
    # Far right rather than against the wordmark. It used to step down to half
    # the window when she shrank into that corner; that read as a stray label
    # mid-screen (2026-10-04, his call), so now it stays put and SHE sits lower:
    # her shrunk head starts at least 84px down, below the label.
    assert "top:56px;right:22px" in css, "it is not where it was put"
    assert "stateLabel.style.top = '56px'" in FACE, "it moves again"
    assert "my = Math.max(canvas.height * 0.10, 84" in FACE, "her head would sit under the label"


def test_the_word_announces_a_change_rather_than_only_the_new_state():
    assert "@keyframes state-turn" in FACE
    fn = FACE[FACE.index("function setState(s, toolName) {"):][:900]
    assert "const changed =" in fn, (
        "the sweep replays on every call, and setState is called repeatedly "
        "with the same state — that is a flicker, not a signal"
    )
    assert "void stateLabel.offsetWidth" in fn, "a CSS animation cannot replay without a reflow"
    assert "prefers-reduced-motion" in FACE


def test_the_way_out_sits_in_the_row_with_the_arrangements():
    """It was alone underneath them, which read as a different kind of thing
    when it is the same kind of thing: one icon among six."""
    build = FACE[FACE.index("function _netBuildLayouts(){"):]
    build = build[:build.index("\n}")]
    assert "_netLay.prepend(_netExitBtn)" in build, "it is still on its own"
    # Prepended AFTER the row's innerHTML is written, or it would be discarded.
    assert build.index("_netLay.innerHTML") < build.index("_netLay.prepend"), (
        "the row is rebuilt after the exit is put in it, which throws it away"
    )


def test_the_fields_labels_do_not_come_along_when_the_page_is_copied():
    """Every node name is its own absolutely-positioned span, so they carry no
    order and no separator in the document. Copying the page ran the whole
    field together into one word, which turned up in a pasted transcript as
    "everythingeverythingDirectHit..." and read like a broken breadcrumb —
    while the breadcrumb was joining its own steps with a slash quite
    correctly. The giveaway was the centre's name appearing twice: once in the
    trail, once as a node."""
    for layer in ("#net-labels{", "#net-hubs{"):
        css = FACE[FACE.index(layer):]
        css = css[:css.index("}")]
        assert "user-select:none" in css, f"{layer} is still copyable prose"
    # The trail IS text and should stay selectable.
    trail = FACE[FACE.index("#net-trail{"):]
    trail = trail[:trail.index("}")]
    assert "user-select:none" not in trail, "the breadcrumb is real text"


def test_the_way_out_is_not_on_screen_before_it_is_built():
    """It joins the arrangements row the first time the field is opened, and
    until then it sits in the column outside it. When its old opacity rule was
    removed on joining the row, it appeared on load as one empty button in the
    bottom-right corner with no icon in it."""
    # The standalone rule, not the shared one: "#net-exit{" is also a substring
    # of "#net-lay button, #net-exit{", which is where it gets display:flex.
    own = FACE.index("#net-exit{margin-right")
    css = FACE[own:FACE.index("}", own) + 1]
    assert "display:none" in css, "it is on screen before it has been built"
    # And it has to come AFTER the shared rule, or display:flex wins on equal
    # specificity and it is visible anyway.
    assert own > FACE.index("#net-lay button, #net-exit{"), (
        "the hiding rule is overridden by the shared one below it"
    )
    build = FACE[FACE.index("function _netBuildExit(){"):]
    build = build[:build.index("\n}")]
    assert "style.display = ''" in build, "built, and then never shown"
    # Shown only after it has something in it.
    assert build.index("innerHTML") < build.index("style.display = ''"), (
        "revealed before the icon is in it, which is the empty button again"
    )
