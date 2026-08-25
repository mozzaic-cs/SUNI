"""
Turning a model-supplied path into one that can actually be written.

Shared by every tool that writes a file the model named. It exists because the
obvious implementation is wrong on Windows in a way that fails loudly at the
worst moment:

    Path("C:/out/a:b.docx")

Windows reads the colon as a DRIVE separator, so Path resolves the parent to
"a:" and the write dies with "cannot find the path specified" — the illegal
character is never cleaned, because Path stopped treating it as part of the
filename. Sanitising `p.stem` therefore cleans a filename that has already been
misparsed. `Path(dir) / "a:b.docx"` is worse still: it discards `dir` entirely
and returns a drive-relative path, so the file lands somewhere else on disk.

Splitting on the separator by hand, before Path is involved, is what makes the
character reachable.
"""
from __future__ import annotations

import re
from pathlib import Path

# Characters that cannot appear in a Windows filename. Separators are in here
# too, which is safe only because the path is split on them first.
_WIN_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def resolve_output_path(path: str, ext: str, user_id: str = "") -> str:
    """Where a generated file should actually be written.

    The model is told "use the user's Desktop by default" and never told what
    that path is, so it invents one. Observed in production: it wrote
    `\\Users\\yourusername\\Desktop\\coimbra_info.pdf` — the literal placeholder
    from the tool description — and because the writer called
    `mkdir(parents=True)`, a `C:\\Users\\yourusername\\Desktop` directory was
    created and the user's file went somewhere they would never look.

    The rule: honour a directory that already EXISTS, because a user who says
    "save it to D:/reports" means it. Otherwise keep only the filename and put
    it in the configured output directory. An invented path therefore lands
    somewhere findable instead of creating a new tree.
    """
    raw = str(path or "").replace("\\", "/")
    parent, _, _name = raw.rpartition("/")
    cleaned = safe_output_path(raw, ext)

    if parent:
        parent_path = Path(parent)
        if parent_path.is_dir():
            return cleaned            # a real directory the caller chose

    # No directory, or one that does not exist: use the configured location.
    try:
        from ..user_settings import resolve_output_dir
        out_dir = resolve_output_dir(user_id)
    except Exception:                 # noqa: BLE001
        out_dir = ""
    if not out_dir:
        return cleaned
    return str(Path(out_dir) / Path(cleaned).name)


def safe_output_path(path: str, ext: str) -> str:
    """Clean the final component of `path` and force its extension to `ext`.

    The extension is forced rather than preserved so a caller that passes
    "report.pdf" while asking for a spreadsheet cannot produce a file whose name
    lies about its contents.
    """
    ext = ext.lower().lstrip(".")
    raw = str(path).replace("\\", "/")
    parent, _, name = raw.rpartition("/")
    name = _WIN_INVALID.sub("_", name).strip(". _")
    stem = name.rsplit(".", 1)[0] if "." in name else name
    stem = stem.strip(". _") or "document"
    filename = f"{stem}.{ext}"
    return str(Path(parent) / filename) if parent else filename
