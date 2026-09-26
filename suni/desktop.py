"""What is on the screen, so SUNI can answer about the work in front of you.

Window titles are the cheapest useful signal on a desktop and the richest for
their size: "ISOBLEND — Gestão da Qualidade", "Orçamento_2026.xlsx", "Inbox —
Gmail". A single sample says what you are working on without a screenshot, a
vision model or a frame of video.

They are also, for the same reason, somebody's day in plain text — client names,
document names, an email address in a mail window's title. So:

  OFF unless switched on. Like meeting recording, the setting only DISABLES;
  nothing here starts because a feature shipped.

  ONE NAMED PERSON sees it. SUNI is multi-user and this is one machine's screen.
  Without desktop_owner naming a user, nothing is reported to anyone — the
  colleague on the other end of a shared instance is not entitled to watch the
  operator work.

  EXCLUSIONS ARE APPLIED BEFORE ANYTHING LEAVES. A password manager's window is
  dropped where it is read, not filtered downstream, so there is no path where
  it exists in memory as context.

  NOTHING IS WRITTEN DOWN. A short in-memory ring, replaced as it samples. The
  screen is a live signal, not a record; if it were a record it would need
  retention, erasure and export like everything else in here.
"""
from __future__ import annotations

import logging
import platform
import re
import time
from typing import Any

_log = logging.getLogger("suni.desktop")

MAX_WINDOWS = 12
_RING_MAX = 8
_ring: list[dict] = []          # recent snapshots, newest last. Memory only.
_last_sample = 0.0

# Windows whose titles are never worth the risk, matched case-insensitively
# against both the process name and the title.
DEFAULT_EXCLUDE = (
    "keepass", "1password", "bitwarden", "lastpass", "dashlane", "keeper",
    "credential", "password", "seahorse", "gnome-keyring",
)


def enabled(config=None) -> bool:
    cfg = config if config is not None else _cfg()
    return bool(cfg.get("desktop_awareness"))


def owner(config=None) -> str:
    cfg = config if config is not None else _cfg()
    return str(cfg.get("desktop_owner") or "").strip()


def visible_to(user_id: str, config=None) -> bool:
    """Whether THIS caller may be told what is on the screen.

    Both halves must hold: the feature is on, and the caller is the person whose
    machine it is. An unset owner means nobody, deliberately — a shared instance
    must not quietly report one person's desktop to another.
    """
    own = owner(config)
    return bool(enabled(config) and own and user_id and user_id == own)


def _cfg():
    from . import config as _c
    return _c


def _excludes(config=None) -> tuple[str, ...]:
    cfg = config if config is not None else _cfg()
    extra = cfg.get("desktop_exclude") or []
    if isinstance(extra, str):
        extra = [extra]
    return tuple(str(x).lower() for x in list(DEFAULT_EXCLUDE) + list(extra) if str(x).strip())


def _blocked(app: str, title: str, excludes: tuple[str, ...]) -> bool:
    hay = f"{app} {title}".lower()
    return any(x in hay for x in excludes)


# ── reading the screen ───────────────────────────────────────────────────────

def _windows_win32(excludes: tuple[str, ...]) -> tuple[dict | None, list[dict]]:
    import ctypes
    import ctypes.wintypes as wintypes

    user32 = ctypes.windll.user32
    try:
        import psutil
    except Exception:      # noqa: BLE001
        psutil = None

    def _name_of(hwnd) -> str:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not psutil:
            return ""
        try:
            return psutil.Process(pid.value).name()
        except Exception:      # noqa: BLE001 — a process can die between calls
            return ""

    def _title_of(hwnd) -> str:
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    out: list[dict] = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _title_of(hwnd)
        if not title:
            return True
        app = _name_of(hwnd)
        if not _blocked(app, title, excludes):
            out.append({"app": app, "title": title})
        return True

    try:
        user32.EnumWindows(CB(_cb), 0)
    except Exception as exc:      # noqa: BLE001
        _log.debug("[DESKTOP] enumerate failed: %s", exc)

    focused = None
    try:
        # Can legitimately be nothing: a locked screen, or no window holding
        # focus. The list of what is open is still worth having, so a missing
        # foreground window is not treated as a failure.
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            title = _title_of(hwnd)
            app = _name_of(hwnd)
            if title and not _blocked(app, title, excludes):
                focused = {"app": app, "title": title}
    except Exception:      # noqa: BLE001
        pass
    return focused, out


def snapshot(config=None) -> dict:
    """What is open right now. Excluded windows never enter the result."""
    excludes = _excludes(config)
    focused, windows = (None, [])
    if platform.system() == "Windows":
        focused, windows = _windows_win32(excludes)
    # Other platforms: no reader yet. Reporting nothing is correct — inventing
    # a guess about somebody's screen would be worse than saying nothing.
    seen, trimmed = set(), []
    for w in windows:
        key = (w["app"], w["title"])
        if key in seen:
            continue
        seen.add(key)
        trimmed.append(w)
    return {"at": time.time(), "focused": focused, "windows": trimmed[:MAX_WINDOWS]}


def sample(config=None) -> dict:
    """Take a snapshot if the last one is stale, and keep a short history."""
    global _last_sample
    cfg = config if config is not None else _cfg()
    every = float(cfg.get("desktop_sample_s") or 6)
    now = time.time()
    if _ring and (now - _last_sample) < every:
        return _ring[-1]
    snap = snapshot(cfg)
    _last_sample = now
    _ring.append(snap)
    del _ring[:-_RING_MAX]
    return snap


def clear() -> None:
    """Forget everything sampled. Called when the feature is switched off."""
    _ring.clear()


def _short(title: str, limit: int = 70) -> str:
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def context_line(user_id: str, config=None) -> str:
    """One line of background for a turn, or "" when it is not this caller's to see.

    Background, not instruction, and it says so: a reply that recites the user's
    open windows back at them is creepy and useless in equal measure.
    """
    if not visible_to(user_id, config):
        return ""
    snap = sample(config)
    focused, windows = snap.get("focused"), snap.get("windows") or []
    if not focused and not windows:
        return ""
    bits = []
    if focused:
        bits.append(f"in front of them: {_short(focused['title'])}"
                    + (f" ({focused['app']})" if focused.get("app") else ""))
    others = [w for w in windows if not focused or w["title"] != focused["title"]][:6]
    if others:
        bits.append("also open: " + "; ".join(_short(w["title"], 46) for w in others))
    return ("[Background — what is on this person's screen right now. "
            + ". ".join(bits)
            + ". Use it only if it makes your answer more useful; they did not "
              "ask about it, and listing their windows back at them is not an "
              "answer. Do not quote or mention this note.]")
