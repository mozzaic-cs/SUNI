"""The orb page ("/") is face.html with the orb in place of her head, so it
has every feature the face page has (dock, caption, agents, stage, network,
voice) and the two cannot drift apart."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
FACE = (ROOT / "suni/web/face.html").read_text(encoding="utf-8")
SERVER = (ROOT / "suni/web/server.py").read_text(encoding="utf-8")


def test_the_root_serves_the_face_page_in_orb_mode():
    assert 'FACE_FILE.read_text(encoding="utf-8").replace("__SUNI_PAGE__", "orb")' in SERVER
    assert "const ORB_MODE = '__SUNI_PAGE__' === 'orb';" in FACE
    assert not (ROOT / "suni/web/ui.html").exists()


def test_the_orb_takes_the_heads_place():
    assert "if (ORB_MODE) { _drawOrb(dt, _fovT); return; }" in FACE
    # no head model downloaded for the orb
    assert FACE.count("  if (ORB_MODE) return;\n  fetch(") == 2


def test_rejected_looks_stay_out():
    """A grid, rings and motes read as a globe or an atom; no glitches."""
    orb = FACE[FACE.index("const ORB_FS = `"):FACE.index("`;", FACE.index("const ORB_FS = `"))]
    for gone in ("float grid(", "vec3 ring(", "motes", "glitch"):
        assert gone not in orb


def test_speaking_ripples_the_surface_on_syllables():
    """Each syllable sends a ripple across the surface, in the normals (3D),
    chosen over an outline bending with the voice and rings of inner light."""
    assert "float rippleSlope(" in FACE and "vec3 Nl = normalize(N - tTh * slope" in FACE
    assert "T - _orbLastRing > 0.25" in FACE, "a ring per frame, not per syllable"


def test_no_scan_line_while_thinking():
    orb = FACE[FACE.index("const ORB_FS = `"):FACE.index("`;", FACE.index("const ORB_FS = `"))]
    assert "scanY" not in orb
