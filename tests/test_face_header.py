"""The strip along the top of the Face: what is answering, and on what terms.

Two things live up there that a viewer reads as facts about the system — which
model is thinking, and how much rope it has this turn. Both were wrong in their
own way: the model chip printed the configured primary whatever was actually
answering, and the mode picker offered four words with no consequences attached
to any of them.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
FACE = (ROOT / "suni/web/face.html").read_text(encoding="utf-8")
SERVER = (ROOT / "suni/web/server.py").read_text(encoding="utf-8-sig")
ORCH = (ROOT / "suni/core/orchestrator.py").read_text(encoding="utf-8")


# ── the model chip ──────────────────────────────────────────────────────────

def test_the_chip_reports_what_answers_not_what_is_configured():
    """SUNI_MODEL is resolved once at import. Two settings send the turn
    somewhere else entirely, and while the chip printed the configured name
    regardless, the honest answer to "which model is this?" was on screen and
    wrong — which is what sent us chasing a model that was not in use.
    """
    body = SERVER[SERVER.index('@app.get("/api/status"'):]
    body = body[:body.index("@app.get", 40)]
    assert "force_claude_code" in body, "the chip cannot see the override"
    assert "model_chain_routing" in body, "the chip cannot see chain routing"
    assert '"answering"' in body and '"model_note"' in body
    assert "suni_config.all()" in body, (
        "the status is read from a startup constant, so a live config change "
        "would not reach it"
    )


def test_the_page_prefers_what_answers_and_says_when_it_differs():
    assert "d.answering || d.model" in FACE, "the page still prints only the configured name"
    assert "routing_override" in FACE, "nothing marks a chip that is not the configured model"
    assert ".mtag.override{" in FACE, "the marked state has no styling"


def test_the_chip_is_readable():
    """--dim is 42% alpha; at 8.5px these were labels you had to go looking
    for, and they carry the two facts most worth glancing at."""
    css = FACE[FACE.index(".mtag{"):]
    css = css[:css.index("}")]
    size = float(re.search(r"font-size:([\d.]+)px", css).group(1))
    assert size >= 9, f"{size}px is too small to read at a glance"
    alpha = float(re.search(r"color:rgba\([\d\s,]+,\s*([\d.]+)\)", css).group(1))
    assert alpha >= 0.7, f"alpha {alpha} is faded to the point of decoration"


# ── the modes ───────────────────────────────────────────────────────────────

def _offered_modes() -> set[str]:
    sel = FACE[FACE.index('<select id="mode-select"'):]
    sel = sel[:sel.index("</select>")]
    return set(re.findall(r'<option value="([\w-]+)"', sel))


def test_every_offered_mode_is_one_the_orchestrator_implements():
    """A picker listing a mode nothing acts on is a switch wired to nothing."""
    for mode in _offered_modes() - {"assistant"}:
        assert f'conv_mode == "{mode}"' in ORCH, (
            f'the picker offers "{mode}" but nothing in the orchestrator reads it'
        )


def test_there_is_help_and_it_covers_every_mode():
    """Four words and no consequences is not a choice anybody can make."""
    assert 'id="mode-help"' in FACE, "there is no way to ask what the modes do"
    panel = FACE[FACE.index('id="mode-help-panel"'):]
    panel = panel[:panel.index("</div>")]
    for mode in _offered_modes():
        assert f'data-m="{mode}"' in panel, f'the help never explains "{mode}"'


def test_the_help_says_what_changes_not_what_the_word_means():
    """Task mode changes the contract — she stops and waits for you to type
    approve — and finding that out by watching her appear to hang is the wrong
    way to learn it."""
    panel = FACE[FACE.index('id="mode-help-panel"'):]
    panel = panel[:panel.index("</div>")]
    assert "approve" in panel, "the help never mentions that task mode waits for you"
    assert "cancel" in panel
    assert "MCP" in panel, "the help does not say read-only switches MCP tools off"


def test_the_help_only_appears_when_there_is_a_choice_to_explain():
    """An install where the role allows one mode has nothing to explain, and
    the picker is hidden there too."""
    init = FACE[FACE.index("function _modeHelpInit("):]
    init = init[:init.index("\n}")]
    assert "btn.style.display = ''" in init, "the button never becomes visible"
    # It is turned on from inside the branch that shows the picker.
    show = FACE[FACE.index("sel.style.display = '';"):][:1800]
    assert "_modeHelpInit()" in show, (
        "the help shows even when the picker does not, so it explains a choice "
        "the viewer does not have"
    )


def test_the_help_can_be_dismissed():
    init = FACE[FACE.index("function _modeHelpInit("):]
    init = init[:init.index("\n}")]
    assert "'Escape'" in init and "document.addEventListener('click'" in init
    assert "e.stopPropagation()" in init, (
        "a click inside the panel would close it, so it cannot be read"
    )


# ── the notice ──────────────────────────────────────────────────────────────

def _toast_css() -> str:
    a = FACE.index("#suni-toast{")
    return FACE[a:FACE.index("@keyframes toast-in")]


def test_the_notice_is_in_the_middle_and_readable():
    """It was a small bar tucked against the bottom edge at 12px."""
    css = _toast_css()
    assert "position:fixed;left:50%;top:42%" in css, "the notice is not centred"
    size = float(re.search(r"font-size:([\d.]+)px", css).group(1))
    assert size >= 15, f"{size}px is the size it already was"


def test_red_is_a_kind_it_is_asked_for_not_the_only_one_there_is():
    """The bar was red whatever it carried, so "settings saved" and "mode
    changed" both read as something having gone wrong."""
    fn = FACE[FACE.index("function _showToast(msg, kind)"):]
    fn = fn[:fn.index("\n}")]
    assert "kind === 'warn'" in fn, "the notice has only one appearance"
    assert "#suni-toast.warn{" in FACE, "the warning kind has no styling"
    # And the things that actually failed are the ones asking for it.
    for failure in ("orb.mic_blocked", "I cannot reach the microphone.",
                    "I could not transcribe that.", "orb.settings_error"):
        i = FACE.index(failure)
        assert "'warn'" in FACE[i:i + 120], f"{failure!r} no longer reads as a failure"
    # ...and the things that did not, are not. Bounded to that line: a fixed
    # window ran into the settings_error call on the line below, which asks
    # for red quite correctly.
    ok = FACE.index("orb.settings_saved")
    line = FACE[ok:FACE.index("\n", ok)]
    assert "'warn'" not in line, "a success still arrives in red"


def test_it_arrives_and_leaves_rather_than_appearing_and_vanishing():
    assert "@keyframes toast-in" in FACE and "@keyframes toast-out" in FACE
    assert "@keyframes toast-sweep" in FACE, "no materialise sweep"
    assert "backdrop-filter:blur" in _toast_css(), "it is not translucent"
    fn = FACE[FACE.index("function _showToast(msg, kind)"):]
    fn = fn[:fn.index("\n}")]
    assert "classList.add('out')" in fn, "it vanishes instead of leaving"


def test_one_notice_at_a_time():
    """Two of these overlapping in the middle of the screen is worse than the
    second one waiting."""
    fn = FACE[FACE.index("function _showToast(msg, kind)"):]
    fn = fn[:fn.index("\n}")]
    assert "if (_toastEl) _toastEl.remove()" in fn
    assert "clearTimeout(_toastT)" in fn, "a stale timer would remove the new one"


def test_a_long_notice_stays_long_enough_to_read():
    fn = FACE[FACE.index("function _showToast(msg, kind)"):]
    fn = fn[:fn.index("\n}")]
    assert "String(msg).length" in fn, (
        "a sentence about what a mode does gets the same time as 'Saved'"
    )


def test_reduced_motion_still_gets_the_message():
    assert "prefers-reduced-motion" in _toast_css() or \
           "prefers-reduced-motion: reduce){\n  #suni-toast" in FACE
    block = FACE[FACE.index("@media (prefers-reduced-motion: reduce){"):][:400]
    assert "#suni-toast" in block and "animation:none" in block
