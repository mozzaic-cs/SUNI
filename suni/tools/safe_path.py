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

import logging
import re
from pathlib import Path

log = logging.getLogger("suni.tools.safe_path")

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


def resolve_attachment_path(path: str, user_id: str = "") -> str:
    """Find a file the model named but could not locate. The mirror image of
    resolve_output_path.

    From live use, and the reason this exists. create_pdf reported exactly where
    it wrote:

        [TOOL] create_pdf   PDF created: C:/Users/<the real user>/Desktop/coimbra_info.pdf

    and the model then asked to attach:

        [TOOL] send_email   Attachment(s) not found: /home/user/Desktop/coimbra_info.pdf

    A POSIX path invented on a Windows box, for a file it had just been told the
    real location of. Telling the model harder does not fix this — a 7B merges
    the shape of a path it has seen a thousand times in training with the one in
    front of it. The filename, though, it got right, and the file is somewhere
    SUNI itself chose to put it.

    So: an existing path is honoured untouched, because a user who says
    "attach D:/reports/q3.pdf" means that file. Only on a miss is the basename
    looked for, and only in the two directories SUNI writes to. Deliberately not
    a wider search — a hunt across the disk would let a hallucinated filename
    attach a document nobody asked to send.

    Returns the original path unchanged when nothing matches, so the caller
    still reports a genuine "not found" rather than silently sending nothing.
    """
    raw = str(path or "")
    if not raw:
        return raw
    try:
        if Path(raw).is_file():
            return raw
    except OSError:
        pass                      # malformed for this platform: fall through

    # Split by hand. Path() would misparse a colon as a drive separator before
    # .name ever ran — the same trap documented at the top of this file.
    name = raw.replace("\\", "/").rpartition("/")[2]
    if not name:
        return raw

    candidates = []
    try:
        from ..user_settings import resolve_output_dir
        out_dir = resolve_output_dir(user_id)
        if out_dir:
            candidates.append(Path(out_dir))
    except Exception:             # noqa: BLE001
        pass
    try:
        candidates.append(Path.home() / "Desktop")
    except (OSError, RuntimeError):
        pass

    for directory in candidates:
        try:
            found = directory / name
            if found.is_file():
                log.info("[ATTACH] path substituted: %s -> %s", raw, found)
                return str(found)
        except OSError:
            continue
    return raw


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
