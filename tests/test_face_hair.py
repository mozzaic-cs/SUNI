"""The hair is shaded onto the head, not a separate model, and stops at a
hairline; off, the head is exactly the bald one."""
import pathlib

FACE = (pathlib.Path(__file__).resolve().parent.parent
        / "suni/web/face.html").read_text(encoding="utf-8")


def test_hair_is_switchable_and_off_means_untouched():
    assert "const HAIR = { on:" in FACE and "window.HAIR = HAIR" in FACE
    assert "gl.uniform1f(uShair, HAIR.on ? HAIR.lift : 0.0)" in FACE
    assert "v_hair = u_hairLift > 0.0 ? 1.0 : 0.0" in FACE


def test_lift_and_shading_share_one_hairline():
    assert FACE.count("${HAIR_GLSL}") == 2, "vertex and fragment stages must agree"
    assert "smoothstep(hairLine(p) - 0.01" in FACE and "float hy = hairLine(v_p)" in FACE


def test_the_on_off_reaches_the_fragment_stage_as_a_varying():
    """A uniform shared by both stages with different precision will not link
    in WebGL 1."""
    assert "varying float v_hair;" in FACE
