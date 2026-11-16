"""A private add-on folder: one instance's own features, outside the public code.

Most of what makes one SUNI someone's own is data (memory/, .env). Some of it
is code — a feature its owner wants and nobody else should get. That code
lives in `personal/` at the repository root, which git ignores and the updater
never touches. It may have its own git history; that is its owner's business.

If `personal/suni_personal/__init__.py` exists, it is imported once, and the
public code calls into it at a few named points. Every name below is optional
in the add-on; a missing one is a no-op:

  SUBJECT_TABLES     extra (db, table, user column, label) rows for erasure
  EXPORT_COLUMNS     {table: columns} for subject access, one per table above
  mount(app, deps)   add HTTP routes; deps holds the auth dependencies
  on_turn(info)      after every user-facing turn, OFF the event loop
  after_upkeep_scan()  after each upkeep scan, in the scan's thread
  page_script()      JavaScript served at /overlay.js on the built-in pages

Without the folder this module does nothing, and a stranger's clone never has
the folder: tests/test_separation.py checks that it is never tracked.

Nothing the add-on does may break SUNI. Every call into it is wrapped: an
exception is logged and swallowed, and a broken add-on is the same as none.
"""
from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

log = logging.getLogger("suni.overlay")

ROOT = Path(__file__).resolve().parent.parent
OVERLAY_DIR = ROOT / "personal"
PACKAGE = "suni_personal"

_module = None
_tried = False


def _load():
    global _module, _tried
    if _tried:
        return _module
    _tried = True
    if not (OVERLAY_DIR / PACKAGE / "__init__.py").is_file():
        return None
    try:
        if str(OVERLAY_DIR) not in sys.path:
            sys.path.insert(0, str(OVERLAY_DIR))
        _module = importlib.import_module(PACKAGE)
        log.info("[OVERLAY] loaded %s from %s", PACKAGE, OVERLAY_DIR)
    except Exception as exc:      # noqa: BLE001 — a broken add-on is no add-on
        log.warning("[OVERLAY] %s did not load: %s", PACKAGE, exc)
        _module = None
    return _module


def active() -> bool:
    return _load() is not None


def _call(name: str, *args, default=None):
    mod = _load()
    fn = getattr(mod, name, None) if mod else None
    if not callable(fn):
        return default
    try:
        return fn(*args)
    except Exception as exc:      # noqa: BLE001
        log.warning("[OVERLAY] %s failed: %s", name, exc)
        return default


def subject_tables() -> tuple[tuple[str, str, str, str], ...]:
    mod = _load()
    rows = getattr(mod, "SUBJECT_TABLES", ()) if mod else ()
    return tuple(tuple(r) for r in rows if len(r) == 4)


def export_columns() -> dict[str, tuple[str, ...]]:
    mod = _load()
    cols = getattr(mod, "EXPORT_COLUMNS", {}) if mod else {}
    return {str(k): tuple(v) for k, v in dict(cols).items()}


def mount(app, deps: dict) -> None:
    _call("mount", app, deps)


def on_turn(info: dict) -> None:
    _call("on_turn", info)


def after_upkeep_scan() -> None:
    _call("after_upkeep_scan")


def page_script() -> str:
    js = _call("page_script", default="")
    return js if isinstance(js, str) else ""
