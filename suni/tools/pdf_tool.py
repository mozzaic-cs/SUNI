"""Generate well-formatted PDF files from text content using fpdf2."""
from __future__ import annotations
import asyncio
import os
import re
from functools import lru_cache
from pathlib import Path

from .safe_path import safe_output_path

# Characters illegal in Windows filenames
_WIN_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# ── Unicode fonts ────────────────────────────────────────────────────────────
# fpdf2's built-in core fonts are Latin-1 only, so without a TrueType font any
# accented text — Portuguese, most of Europe — is mangled or dropped. DejaVu
# covers it and ships with essentially every Linux distribution.
#
# Both the directory AND the filenames differ per platform, which is why a
# single hardcoded path silently produced Latin-1 PDFs off Windows:
#   Windows  C:/Windows/Fonts/DejaVuSansBold.ttf
#   Linux    /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
# so each style lists the spellings seen in the wild and the first hit wins.
_FONT_DIRS = [
    Path("C:/Windows/Fonts"),                       # Windows
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Windows/Fonts",
    Path("/usr/share/fonts"),                       # Linux
    Path("/usr/local/share/fonts"),
    Path.home() / ".fonts",
    Path.home() / ".local/share/fonts",
    Path("/Library/Fonts"),                         # macOS
    Path("/System/Library/Fonts"),
    Path.home() / "Library/Fonts",
]

_FONT_CANDIDATES: dict[tuple[str, str], list[str]] = {
    ("DejaVu",     ""):   ["DejaVuSans.ttf"],
    ("DejaVu",     "B"):  ["DejaVuSans-Bold.ttf", "DejaVuSansBold.ttf"],
    ("DejaVu",     "I"):  ["DejaVuSans-Oblique.ttf", "DejaVuSansOblique.ttf"],
    ("DejaVu",     "BI"): ["DejaVuSans-BoldOblique.ttf", "DejaVuSansBoldOblique.ttf"],
    ("DejaVuMono", ""):   ["DejaVuSansMono.ttf", "DejaVuMonoSans.ttf"],
    ("DejaVuMono", "B"):  ["DejaVuSansMono-Bold.ttf", "DejaVuMonoSansBold.ttf"],
}


@lru_cache(maxsize=1)
def _font_map() -> dict[tuple[str, str], Path]:
    """Locate each font file on this machine. Cached: the search walks font
    directories, which is cheap but pointless to repeat per document.

    A style with no file of its own falls back to the regular weight of the
    same family. That keeps text upright rather than italic, but registering
    the style is what matters: asking fpdf2 for an unregistered style raises,
    so the alternative is a crash on any document that uses italics. Many
    distributions ship DejaVu without the oblique faces, so this is the common
    case on Linux, not an edge case.
    """
    found: dict[tuple[str, str], Path] = {}
    for key, names in _FONT_CANDIDATES.items():
        for directory in _FONT_DIRS:
            if not directory.is_dir():
                continue
            for name in names:
                direct = directory / name
                if direct.is_file():
                    found[key] = direct
                    break
                # Linux nests fonts under per-family subdirectories
                match = next(directory.rglob(name), None)
                if match:
                    found[key] = match
                    break
            if key in found:
                break

    for (family, style) in _FONT_CANDIDATES:
        if (family, style) not in found and (family, "") in found:
            found[(family, style)] = found[(family, "")]
    return found

SCHEMA = {
    "name": "create_pdf",
    "description": (
        "Create a PDF file from text content and save it to a specified path. "
        "Use this whenever the user asks to put information in a PDF or create a document. "
        "For the path, use the user's Desktop by default "
        "unless they specify elsewhere."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The text content to put in the PDF"},
            "path":    {"type": "string", "description": "Full file path for the PDF"},
            "title":   {"type": "string", "description": "Optional document title"},
        },
        "required": ["content", "path"],
    },
}

# ── Margins ──────────────────────────────────────────────────────────────────
_LM, _RM, _TM, _BM = 20, 20, 18, 18      # left, right, top, bottom (mm)
_PW = 210 - _LM - _RM                      # usable page width = 170 mm

# ── Colours  (R, G, B) ───────────────────────────────────────────────────────
_NAVY   = (15,  45, 100)
_BLUE   = (30,  80, 150)
_DGREY  = (50,  50,  50)
_MGREY  = (110, 110, 110)
_BGREY  = (240, 243, 248)   # code cell background


def _enc(text: str) -> str:
    """Strip markdown formatting — Unicode is passed through as-is (TrueType fonts)."""
    t = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)   # [label](url)
    t = re.sub(r'\*{1,2}([^*\n]+)\*{1,2}', r'\1', t)   # **bold**
    t = re.sub(r'`([^`]+)`', r'\1', t)                  # `code`
    t = re.sub(r'#{1,6}\s+', '', t)                     # ## heading
    return t.strip()


def _is_rule(s: str) -> bool:
    return bool(re.match(r'^[=\-]{3,}\s*$', s))

def _is_h1(s: str) -> bool:
    return bool(re.match(r'^\d+\.\s+\S', s)) and len(s) < 80

def _is_allcaps(s: str) -> bool:
    clean = re.sub(r'[^A-Za-z]', '', s)
    return len(clean) >= 5 and clean == clean.upper() and not s.startswith('-')

def _is_h3(s: str) -> bool:
    return (s.endswith(':') and 8 < len(s) < 70
            and s[0].isupper()
            and ',' not in s[:20]
            and not re.match(r'^\s*[-*•]', s)
            and '  ' not in s[:20])

def _is_bullet(s: str) -> bool:
    return bool(re.match(r'^[-*•]\s+', s))

def _is_code(s: str) -> bool:
    return bool(re.match(
        r'^suni/|^  \S|.*\.(py|json|faiss|log|txt|csv|docx|xlsx|md)\s*([-–—]|$)',
        s, re.I))


# ── PDF class ─────────────────────────────────────────────────────────────────

class _SuniPDF:
    """fpdf2 wrapper using DejaVu TrueType fonts for full Unicode support."""

    def __init__(self, doc_title: str):
        from fpdf import FPDF

        class _PDF(FPDF):
            _doc_title = doc_title

            def header(self_):
                if self_.page <= 1:
                    return
                self_.set_xy(_LM, 8)
                self_.set_font("DejaVu", "I", 7)
                self_.set_text_color(*_MGREY)
                self_.cell(_PW, 4, _enc(self_._doc_title[:80]), align="R")
                self_.set_draw_color(*_MGREY)
                self_.set_line_width(0.15)
                self_.line(_LM, 13, _LM + _PW, 13)
                self_.set_y(17)

            def footer(self_):
                self_.set_y(-12)
                self_.set_x(_LM)
                self_.set_font("DejaVu", "I", 7)
                self_.set_text_color(*_MGREY)
                # EU AI Act Art. 50(2): visible disclosure that this is AI-generated,
                # alongside the page number. Left = disclosure, centre = page no.
                self_.cell(_PW / 2, 4, "AI-generated by SUNI", align="L")
                self_.set_x(_LM)
                self_.cell(_PW, 4, str(self_.page), align="C")

        self.pdf = _PDF(format='A4')

        # Register DejaVu TrueType fonts — full Unicode including accented chars
        for (name, style), path in _font_map().items():
            if path.exists():
                self.pdf.add_font(name, style=style, fname=str(path))

        # EU AI Act Art. 50(2): machine-readable marking that the content is
        # artificially generated, embedded in the PDF /Info metadata dictionary.
        self.pdf.set_creator("SUNI (AI assistant) — artificially generated")
        self.pdf.set_producer("SUNI AI orchestration framework")
        self.pdf.set_subject("AI-generated document")
        self.pdf.set_keywords("AI-generated, artificially-generated, SUNI, EU-AI-Act-Art-50")

        self.pdf.set_auto_page_break(auto=True, margin=_BM + 8)
        self.pdf.set_margins(_LM, _TM, _RM)
        self.pdf.add_page()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _reset_x(self):
        self.pdf.set_x(_LM)

    def _ln(self, h: float = 3.0):
        self.pdf.ln(h)

    # ── title block ──────────────────────────────────────────────────────────

    def title_block(self, title: str, subtitle: str = ""):
        pdf = self.pdf

        # accent bar
        pdf.set_fill_color(*_BLUE)
        pdf.set_xy(_LM, _TM)
        pdf.rect(_LM, _TM, _PW, 1.5, 'F')
        pdf.ln(6)

        # title
        pdf.set_x(_LM)
        pdf.set_font("DejaVu", "B", 17)
        pdf.set_text_color(*_NAVY)
        pdf.multi_cell(_PW, 9, _enc(title), align="L")
        pdf.ln(2)

        # subtitle
        if subtitle:
            pdf.set_x(_LM)
            pdf.set_font("DejaVu", "", 9)
            pdf.set_text_color(*_MGREY)
            pdf.multi_cell(_PW, 5, _enc(subtitle), align="L")
            pdf.ln(1)

        # rule
        pdf.set_draw_color(*_MGREY)
        pdf.set_line_width(0.3)
        y = pdf.get_y() + 2
        pdf.line(_LM, y, _LM + _PW, y)
        pdf.ln(7)

    # ── heading levels ───────────────────────────────────────────────────────

    def h1(self, text: str):
        pdf = self.pdf
        self._ln(5)
        pdf.set_x(_LM)
        pdf.set_font("DejaVu", "B", 12)
        pdf.set_text_color(*_NAVY)
        pdf.multi_cell(_PW, 7, _enc(text), align="L")
        y = pdf.get_y()
        pdf.set_draw_color(*_BLUE)
        pdf.set_line_width(0.5)
        pdf.line(_LM, y, _LM + _PW * 0.55, y)
        self._ln(3)

    def h2(self, text: str):
        self._ln(4)
        self.pdf.set_x(_LM)
        self.pdf.set_font("DejaVu", "B", 11)
        self.pdf.set_text_color(*_BLUE)
        self.pdf.multi_cell(_PW, 6.5, _enc(text), align="L")
        self._ln(1)

    def h3(self, text: str):
        self._ln(3)
        self.pdf.set_x(_LM)
        self.pdf.set_font("DejaVu", "BI", 10)
        self.pdf.set_text_color(*_BLUE)
        self.pdf.multi_cell(_PW, 6, _enc(text), align="L")
        self._ln(1)

    # ── body ─────────────────────────────────────────────────────────────────

    def body(self, text: str):
        self.pdf.set_x(_LM)
        self.pdf.set_font("DejaVu", "", 10)
        self.pdf.set_text_color(*_DGREY)
        self.pdf.multi_cell(_PW, 5.5, _enc(text), align="L")

    def bullet(self, text: str, indent_mm: float = 0):
        pdf = self.pdf
        if pdf.get_y() + 14 > pdf.h - _BM:
            pdf.add_page()

        x0 = _LM + indent_mm
        bw = 4.5
        tw = _PW - indent_mm - bw

        y = pdf.get_y()
        pdf.set_xy(x0, y)
        pdf.set_font("DejaVu", "", 10)
        pdf.set_text_color(*_BLUE)
        pdf.cell(bw, 5.5, "•")
        pdf.set_xy(x0 + bw, y)
        pdf.set_font("DejaVu", "", 10)
        pdf.set_text_color(*_DGREY)
        pdf.multi_cell(tw, 5.5, _enc(text), align="L")

    def code(self, text: str):
        pdf = self.pdf
        pdf.set_x(_LM)
        pdf.set_fill_color(*_BGREY)
        pdf.set_font("DejaVuMono", "", 8.5)
        pdf.set_text_color(60, 60, 60)
        pdf.multi_cell(_PW, 5, _enc(text), fill=True, align="L")

    def spacer(self, h: float = 3.0):
        self.pdf.ln(h)

    # ── output ───────────────────────────────────────────────────────────────

    def save(self, path: str) -> str:
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.pdf.output(str(dest))
        kb = round(dest.stat().st_size / 1024, 1)
        return f"PDF created: {dest} ({kb} KB)"


# ── Renderer ─────────────────────────────────────────────────────────────────

def _create_pdf_sync(content: str, path: str, title: str = "") -> str:
    doc = _SuniPDF(title or "SUNI Document")

    if title:
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        subtitle = ""
        if lines:
            first = lines[0]
            if (first != title.strip() and len(first) < 90
                    and not _is_h1(first) and not _is_allcaps(first)):
                subtitle = first
        doc.title_block(title, subtitle)

    for raw in content.splitlines():
        s = raw.strip()

        if not s or _is_rule(s):
            doc.spacer(2)
            continue

        if title and s == title.strip():
            continue

        if _is_h1(s):
            doc.h1(s)
            continue

        if _is_allcaps(s) and not s[0].isdigit():
            doc.h2(s)
            continue

        if _is_h3(s):
            doc.h3(s)
            continue

        if re.match(r'^\s{2,}[-*•]\s+', raw):
            text = re.sub(r'^\s*[-*•]\s+', '', s)
            doc.bullet(text, indent_mm=5)
            continue

        if _is_bullet(s):
            text = re.sub(r'^[-*•]\s+', '', s)
            doc.bullet(text)
            continue

        if _is_code(s):
            doc.code(s)
            continue

        doc.body(s)

    return doc.save(path)


def _sanitize_path(path: str) -> str:
    """Sanitise the filename component so it is valid on Windows.

    Delegates to the shared helper: sanitising `p.stem` cleaned a filename that
    Path had already misparsed, because Windows reads a colon in a name as a
    drive separator. See suni/tools/safe_path.py.
    """
    return safe_output_path(path, 'pdf')


async def handler(content: str, path: str, title: str = "") -> str:
    path = _sanitize_path(path)
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _create_pdf_sync, content, path, title)
    except Exception as e:
        return f"PDF creation failed: {e}"
