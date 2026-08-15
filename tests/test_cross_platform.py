"""
Platform assumptions that would break SUNI off Windows.

SUNI was developed on Windows and is published to run anywhere. The failures
that class produces are quiet: an ImportError at startup, or a PDF that renders
accented text as mojibake because no Unicode font was found. Both happened.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest


# ── PDF fonts ────────────────────────────────────────────────────────────────

def test_font_search_is_not_windows_only():
    """The font directory list must cover Linux and macOS.

    Regression: _FONTS_DIR was hardcoded to C:/Windows/Fonts, and registration
    was guarded by path.exists() — so off Windows nothing registered, fpdf2 fell
    back to its Latin-1 core fonts, and every accented character was mangled
    with no error anywhere.
    """
    from suni.tools import pdf_tool
    # as_posix(): str(Path("/usr/share/fonts")) yields backslashes on Windows
    dirs = [d.as_posix() for d in pdf_tool._FONT_DIRS]
    assert any("/usr/share/fonts" in d for d in dirs), "no Linux font directory"
    assert any("Library/Fonts" in d for d in dirs), "no macOS font directory"
    assert any("Windows/Fonts" in d for d in dirs), "no Windows font directory"


def test_font_filenames_cover_both_naming_conventions():
    """Windows and Linux name the same face differently."""
    from suni.tools import pdf_tool
    bold = pdf_tool._FONT_CANDIDATES[("DejaVu", "B")]
    assert "DejaVuSans-Bold.ttf" in bold, "missing the Linux/upstream spelling"
    assert "DejaVuSansBold.ttf" in bold, "missing the Windows spelling"


def test_every_style_resolves_when_a_regular_face_exists():
    """Styles fall back to the regular weight rather than going unregistered.

    Asking fpdf2 for a style that was never registered raises, so a distribution
    shipping DejaVu without the oblique faces — the common case on Linux — would
    otherwise crash any document using italics.
    """
    from suni.tools.pdf_tool import _font_map, _FONT_CANDIDATES
    found = _font_map()
    if ("DejaVu", "") not in found:
        pytest.skip("no DejaVu installed on this machine")
    for key in _FONT_CANDIDATES:
        family, _ = key
        if (family, "") in found:
            assert key in found, f"{key} unresolved despite a regular face being present"


def test_generated_pdf_embeds_a_unicode_font(tmp_path):
    """End-to-end: accented text must produce an embedded TrueType font."""
    from suni.tools.pdf_tool import _SuniPDF, _font_map
    if ("DejaVu", "") not in _font_map():
        pytest.skip("no DejaVu installed on this machine")

    pdf = _SuniPDF("Acentuação")
    pdf.pdf.add_page()
    pdf.pdf.set_font("DejaVu", "", 12)
    pdf.pdf.multi_cell(170, 6, "Ação, coração — português: ÁÉÍÓÚ àâãç ñ €")
    out = tmp_path / "accents.pdf"
    pdf.pdf.output(str(out))

    raw = out.read_bytes()
    assert b"DejaVu" in raw, \
        "no TrueType font embedded — this PDF fell back to Latin-1 core fonts"


# ── no new hardcoded platform paths ──────────────────────────────────────────

SRC = Path(__file__).resolve().parent.parent / "suni"

# Paths that are legitimately Windows-shaped: user-facing examples and the
# candidate lists that exist precisely to be searched per platform.
ALLOWED = ("mcp_catalog.py", "pdf_tool.py")


def test_no_hardcoded_windows_paths_on_runtime_code_paths():
    """A drive letter in logic (not in an example or a search list) will not
    resolve off Windows."""
    offenders = []
    for py in SRC.rglob("*.py"):
        if "__pycache__" in py.parts or py.name in ALLOWED:
            continue
        in_docstring = False
        for i, line in enumerate(py.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            stripped = line.strip()
            # Track triple-quoted blocks: comments and docstrings legitimately
            # describe platform paths (including the ones being warned about).
            fences = stripped.count('"""') + stripped.count("'''")
            if fences % 2 == 1:
                in_docstring = not in_docstring
                continue
            if in_docstring or stripped.startswith("#"):
                continue
            if re.search(r'["\'][A-Za-z]:[\\/]', line):
                offenders.append(f"{py.relative_to(SRC.parent)}:{i}: {stripped[:70]}")
    assert not offenders, "hardcoded Windows paths in runtime code:\n  " + "\n  ".join(offenders)
