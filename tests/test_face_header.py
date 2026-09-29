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
