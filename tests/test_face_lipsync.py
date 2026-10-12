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


def test_lips_stay_parted_while_talking():
    """They meet on "m", "b", "p" and in a real pause, not between every
    syllable: closing on every quiet moment left only the seam line moving."""
    assert "Math.max(mT, 0.14)" in FACE
    assert "_lip.quiet < 0.25" in FACE


def test_a_closure_fires_once_per_dip():
    """In a pause it re-fired every 70 ms and the mouth saw-toothed."""
    assert "!_lip.low && _lip.peak" in _fn("_lipTarget")


def test_the_lip_crease_shows_only_when_the_lips_meet():
    """The bright line where the lips meet was wanted, but it showed all the
    time; hiding it everywhere (or painting the opening dark) was worse."""
    line = next(l for l in FACE.splitlines() if l.strip().startswith("fres *= 1.0 - 0.92"))
    # By the mouth as a whole: per point, the still-touching corners of a
    # round mouth glinted.
    assert "mopen" in line and "float mopen = 1.0 - u_mFx.z" in FACE,         "the crease glow is not tied to the lips meeting"
    # faded over time: judged per frame it snapped on between two frames
    assert "cdt * 6" in FACE


def test_the_lips_part_at_their_junction_not_through_the_lower_lip():
    """At y -0.58 the split ran 0.013 down the lower lip: its skin was
    stretched and darkened into a rectangle over the lips."""
    y = float(re.search(r"const MOUTH = \{ y:(-?[\d.]+)", FACE).group(1))
    assert abs(y - (-0.567)) < 0.003
    assert "m_ny = a_nrm.y" in FACE, "the crease is split by height alone"


def test_only_the_gap_between_the_lips_is_darkened():
    assert "abs(v_ms)" in FACE and "v_ms = 1.0 - 2.0 * m_below" in FACE


def test_the_jaw_drops_below_the_lips_only():
    """Height alone gave the upper lip 0.62 of the jaw: the chin hardly moved."""
    assert "jw = mix(a_jaw, m_below," in FACE and "vec3 p=jr(pm,jw);" in FACE


def test_open_mouths_show_no_teeth_and_no_cup_rim():
    """Painted on the gap, the teeth read as a band (judged 2026-10-12)."""
    assert "float teeth =" not in FACE
    assert "* edge * clamp(v_gap" not in FACE, "the light rim under the lower lip read as a cup"


def test_f_and_v_have_their_own_shape():
    assert "u_mB.w * (0.016 * lipLo" in FACE and "window._mouthPose = (open, wide, fv)" in FACE


def test_no_dark_patches_beside_the_corners():
    """Cutting the rim glow over the whole mouth region (v_mreg) darkened a
    rectangle of cheek beside each corner, and the lifted upper lip's
    underside drew a black band with dark wedges at the corners."""
    assert "max(sx * smoothstep(0.0, 0.2, v_mhx)," in FACE
    assert "smoothstep(0.0, 0.5, v_ms)" in FACE, "the underside of the upper lip is left unlit"
