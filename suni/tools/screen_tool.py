"""Look at the screen — once, when asked, and never on a timer.

The background line from suni/desktop.py says what is OPEN. This says what is
actually there: the figure in the spreadsheet, the error in the dialog, the
paragraph being argued over. It is the difference between "Excel is open" and
"the total in row 40 is wrong".

Three things keep it from being surveillance:

  It only happens when asked. There is no sampler and no interval — a capture
  exists because somebody wanted an answer about what they were looking at.
  It is gated by approval, like sending mail or running a shell command, so the
  capture is a decision a person takes rather than one a model takes.
  It obeys the same owner rule as the rest of desktop awareness: without
  desktop_awareness on and desktop_owner naming the caller, it refuses.

The image lands in the caller's own output directory — their file, under their
per-user isolation, deletable like anything else there — and the reply says
where it went, because a screenshot nobody knows exists is the bad version of
this feature.
"""
from __future__ import annotations

import logging
from datetime import datetime

_log = logging.getLogger("suni.tools.screen")

# 4K downscaled to this on the long edge. Big enough to read a spreadsheet cell,
# small enough not to spend a fortune of tokens on a desktop wallpaper.
MAX_EDGE = 1600

SCHEMA = {
    "name": "look_at_screen",
    "description": (
        "Capture what is on the user's screen right now and look at it. Use when "
        "they ask about something they are looking at — a figure, an error, a "
        "document on screen — and the answer is not already in the conversation. "
        "Requires the user's approval each time."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "What you are looking for, shown to the user on the approval card.",
            },
            "monitor": {
                "type": "string",
                "description": (
                    "Which screen: 'active' (default — the one they are working "
                    "on), 'all', or a number from 1. This machine may have "
                    "several; capturing all of them when one would do is both "
                    "slower and more of their desk than the question needs."
                ),
                "default": "active",
            },
        },
        "required": ["reason"],
    },
}


MAX_MONITORS = 6


def _monitors() -> list[tuple[int, int, int, int]]:
    """Every screen's rectangle in virtual-desktop coordinates.

    Coordinates can be negative — a monitor to the left of the primary starts
    below zero — which is why captures use the virtual desktop rather than
    per-screen origins.
    """
    import ctypes
    import ctypes.wintypes as wintypes
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDPIAware()      # or a 4K screen is captured at 96 dpi
    except Exception:      # noqa: BLE001
        pass
    rects: list[tuple[int, int, int, int]] = []
    PROC = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                              ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def _cb(_h, _hdc, lprc, _lp):
        r = lprc.contents
        rects.append((r.left, r.top, r.right, r.bottom))
        return 1

    try:
        user32.EnumDisplayMonitors(0, None, PROC(_cb), 0)
    except Exception as exc:      # noqa: BLE001
        _log.debug("[SCREEN] monitor enumeration failed: %s", exc)
    return rects


def _active_monitor(rects: list) -> int:
    """The screen holding the focused window, or the primary one.

    "Active" is the useful default: with several screens, the answer is almost
    always about the one being worked on, and capturing the other four is more
    of somebody's desk than the question asked for.
    """
    import ctypes
    import ctypes.wintypes as wintypes
    user32 = ctypes.windll.user32
    try:
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
            for i, (l, t, r, b) in enumerate(rects):
                if l <= cx < r and t <= cy < b:
                    return i
    except Exception:      # noqa: BLE001
        pass
    for i, (l, t, _r, _b) in enumerate(rects):
        if l == 0 and t == 0:
            return i
    return 0


def _variation(img) -> float:
    """How much is actually on the frame, 0 for a flat one.

    A locked workstation still lists its windows, so the rest of desktop
    awareness keeps working while the capture comes back as a single dark
    rectangle. Handing that to a model produces a confident description of a
    wallpaper, so it is detected here and said plainly instead.
    """
    small = img.convert("L").resize((64, 36))
    px = list(small.getdata())
    mean = sum(px) / len(px)
    return (sum((p - mean) ** 2 for p in px) / len(px)) ** 0.5


def _capture(path: str, box=None) -> tuple[tuple[int, int], float]:
    from PIL import ImageGrab
    # all_screens keeps the coordinates in virtual-desktop space, which is the
    # only space that can express a monitor sitting left of, or above, the
    # primary one.
    img = ImageGrab.grab(bbox=box, all_screens=True) if box else ImageGrab.grab(all_screens=False)
    w, h = img.size
    longest = max(w, h)
    if longest > MAX_EDGE:
        scale = MAX_EDGE / float(longest)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    img.save(path, "PNG", optimize=True)
    return img.size, _variation(img)


def handler(reason: str = "", monitor: str = "active") -> str:
    import os
    from .. import desktop as _desktop
    from ..tools.registry import USER_ID_CTX
    from ..user_settings import resolve_output_dir

    user_id = USER_ID_CTX.get("")
    if not _desktop.visible_to(user_id):
        # The same refusal whether it is off or somebody else's machine: which
        # of the two it is, is not this caller's business either.
        return ("Looking at the screen is not enabled for this user. It is off "
                "unless desktop awareness is switched on and this account is "
                "named as the machine's owner.")

    out_dir = resolve_output_dir(user_id) or "."
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    rects = _monitors()
    choice = str(monitor or "active").strip().lower()
    if not rects:                       # single screen, or enumeration failed
        wanted = [(None, 1)]
    elif choice == "all":
        wanted = [(r, i + 1) for i, r in enumerate(rects[:MAX_MONITORS])]
    elif choice.isdigit() and 1 <= int(choice) <= len(rects):
        idx = int(choice) - 1
        wanted = [(rects[idx], idx + 1)]
    else:
        idx = _active_monitor(rects)
        wanted = [(rects[idx], idx + 1)]

    saved, blank = [], []
    for box, number in wanted:
        suffix = f"_{number}" if len(wanted) > 1 or len(rects) > 1 else ""
        path = os.path.join(out_dir, f"screen_{stamp}{suffix}.png")
        try:
            size, variation = _capture(path, box)
        except Exception as exc:      # noqa: BLE001 — say why, do not pretend
            _log.warning("[SCREEN] capture failed: %s", exc)
            return f"Could not capture the screen: {exc}"
        if variation < 4.0:
            # A locked workstation, or simply a screen showing nothing. Either
            # way there is nothing to read, and a model handed a flat rectangle
            # will describe a wallpaper with confidence.
            try:
                os.remove(path)
            except OSError:
                pass
            blank.append(number)
            continue
        saved.append((number, path, size))

    if not saved:
        where = "screen" if len(wanted) == 1 else f"screens {', '.join(map(str, blank))}"
        return (f"The {where} came back blank, which is what a locked workstation "
                "captures. Tell them that, and ask them to unlock it if they want "
                "you to look — do not guess at what was on it.")

    _log.info("[SCREEN] captured %d of %d monitor(s) for %s (%s)",
              len(saved), len(rects) or 1, user_id, reason[:60])
    lines = [f"Captured {len(saved)} screen(s) of {len(rects) or 1} on this machine:"]
    for number, path, size in saved:
        lines.append(f"  screen {number}: {size[0]}x{size[1]} — {path}")
    if blank:
        lines.append(f"  (screen{'s' if len(blank) > 1 else ''} "
                     f"{', '.join(map(str, blank))} showed nothing)")
    lines.append("")
    lines.append("Open those files to see what is on their screen, then answer "
                 "their question from what is there. Say what you saw, not that "
                 "you took a screenshot, and mention the images are in their "
                 "output folder if they may want them.")
    return chr(10).join(lines)
