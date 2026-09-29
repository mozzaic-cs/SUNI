"""What reaches her voice, and what is only ever addressed to the page.

A reply can carry instructions meant for the browser rather than for the person
reading it — a `suni-focus` block is a JSON box of pixel coordinates telling the
stage where to draw a region on an image. Only the stage ever stripped it, and
only in stage mode, so everywhere else those coordinates were captioned and read
ALOUD.

The interesting half of this is the streaming path: directives arrive token by
token like everything else, and a JSON object is full of full stops, so a
sentence splitter left to itself will hand `{"box": [120, 340,` to the speech
queue before the closing brace has even arrived.

That is behaviour, not text, so it is checked by running the page's own splitter
over a streamed reply rather than by looking for words in the source. The
JavaScript lives in tests/js/speech_leak.js and is skipped where node is not
installed — the static checks below still hold there.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FACE = (ROOT / "suni/web/face.html").read_text(encoding="utf-8")
SCRIPT = ROOT / "tests/js/speech_leak.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_directive_never_reaches_her_voice():
    """Streamed in seven-character chunks, so the directive is split across
    several tokens, which is the case that actually goes wrong."""
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (
        "something addressed to the page reached the speech queue:\n"
        + r.stdout + r.stderr
    )
    # The prose around the directive has to survive; stripping by removing the
    # whole buffer would also pass a leak test.
    assert "LOST" not in r.stdout, r.stdout


def test_everything_that_speaks_goes_through_one_cleaner():
    """Three places display or speak the reply, and each had its own idea of
    what to strip: the stage removed the directives, the chat removed the serve
    URL, and the voice removed neither."""
    assert "function _speakable(" in FACE
    assert "_parseFocus(text || '').clean" in FACE, (
        "_speakable does not actually remove the directives"
    )
    done = FACE[FACE.index("const _cleanFull = _speakable(full);"):][:900]
    assert "finalMsg(suniWrap, _cleanFull)" in done, "the chat gets the raw reply"
    assert "surfaced ? sentenceBuf : _cleanFull" in done, (
        "the non-streaming path still hands the voice the raw reply — which is "
        "the path Claude Code uses, so it is the usual one here"
    )


def test_the_stage_still_gets_its_directives():
    """It is the one thing that needs them: stripping them before it sees them
    would silently stop the region boxes being drawn."""
    done = FACE[FACE.index("const _cleanFull = _speakable(full);"):][:900]
    assert "_enhanceStage(_displayable(full))" in done, (
        "the stage is handed text with the boxes already removed"
    )


def test_the_stage_keeps_the_finished_reply():
    """showSub is also the per-sentence caption, and in stage mode it REPLACES
    the panel. The full answer was painted and then immediately overwritten by
    its own first sentence, then the second — which reads as the text flashing
    up and vanishing."""
    body = FACE[FACE.index("function showSub(text) {"):][:1400]
    assert "if (_stageFinal) return;" in body, (
        "per-sentence captions still overwrite the finished reply"
    )
    assert "_stageFinal = true;" in FACE, "nothing ever marks the reply as final"
    # And it has to be cleared, or the stage never fills again.
    send = FACE[FACE.index("clearInterval(_waitDrainTimer);   // stop a previous"):][:400]
    assert "_stageFinal = false" in send, "the next turn would find the stage locked"
