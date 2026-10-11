"""Her mouth follows what is said, not only how loud.

The measurements behind each guard are in the comments in face.html; these
only stop a later edit from quietly undoing them.
"""
import pathlib
import re

FACE = (pathlib.Path(__file__).resolve().parent.parent
        / "suni/web/face.html").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    i = FACE.index(f"function {name}(")
    return FACE[i:FACE.index("\n}\n", i)]


def test_the_shape_is_read_from_the_spectrum():
    shape = _fn("_lipShape")
    assert "getFloatFrequencyData" in shape
    # Each cue drives something: shape → wide/round, F1 → how open, hiss → teeth.
    assert "sh.wide" in shape and "sh.open" in shape and "sh.sib" in shape
    assert "mouthWide = sh.wide" in FACE, "the shape is computed and reaches nothing"


def test_formants_are_read_against_the_voice_not_fixed_numbers():
    """A female voice's formants sit ~15% above the male voice the cues were
    measured on; fixed thresholds would read every vowel of hers as "i"."""
    shape = _fn("_lipShape")
    assert re.search(r"sh\.m1\s*\+=", shape) and re.search(r"sh\.m2\s*\+=", shape)


def test_a_hiss_is_not_a_lip_closure():
    """Measured: every quiet "s" was being closed like an "m"."""
    lip = _fn("_lipTarget")
    assert "hiss" in lip.split("{", 1)[0], "the closure rule cannot see the hiss"
    assert "hiss > 0.3" in lip


def test_a_closure_lets_go_when_the_sound_returns():
    """Holding the full 70 ms kept the lips shut on a loud vowel."""
    assert "_lip.hold = 0" in _fn("_lipTarget")


def test_loudness_keeps_the_window_its_timing_was_tuned_on():
    """The FFT needs 1024 samples; the closure thresholds were tuned on 256."""
    assert "analyser.fftSize = 1024" in FACE
    assert "_wave.length-256" in _fn("audioLevel")
