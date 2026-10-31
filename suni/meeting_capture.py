"""The PC's own sound, and knowing when the meeting is over.

Two things the meeting recorder (suni/meetings.py) needs from Windows that
ffmpeg cannot give it.

THE PC'S SOUND
"Stereo Mix" is one sound card's loopback. A call played through a USB headset
or a virtual device (SteelSeries Sonar here) never reaches it, and the
recording is silence. WASAPI loopback captures whatever Windows plays on the
default output, whichever device that is — looked up at each start, so it
follows a switch to the headset. Suni once recorded a Teams webinar this way
(2026-09-30) in a script she wrote on the spot; here it is the same capture
behind the meeting gate (consent, approval card, audit).

ffmpeg has no WASAPI input, so a thread reads the loopback (PyAudioWPatch) and
writes raw PCM into a pipe ffmpeg reads as its first input. WASAPI delivers
nothing while nothing plays; the feeder writes silence for those stretches, or
the PC track would be shorter than the microphone's and the two would drift
apart by every pause in the call.

KNOWING WHEN IT IS OVER
At the start, the windows that look like a call (Teams, Zoom, a Meet tab) are
noted. When all of them have been gone for a while, the recording stops by
itself — as that September script did — so nobody has to remember to say
"stop". A browser tab loses its title when the user switches tabs, so a Meet
anchor gets a much longer grace than an app window. Nothing found at the start:
no automatic stop, and the caller is told so.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time

from .logger import get_logger

log = get_logger("suni.meeting_capture")

# The device name stored in meeting_devices for "whatever Windows plays".
PC_SOUND = "pc-sound"

_FEED_TICK_S = 0.05
_PAD_BEHIND_S = 0.25          # behind the clock by more than this = silence, pad it


# ── the loopback source ──────────────────────────────────────────────────────

class _WasapiLoopback:
    """The default output device's loopback. Opened per recording."""

    def __init__(self):
        import pyaudiowpatch as pa
        self._pa = pa.PyAudio()
        try:
            d = self._pa.get_default_wasapi_loopback()
            self.name = str(d.get("name", ""))
            self.rate = int(d["defaultSampleRate"])
            self.channels = int(d["maxInputChannels"]) or 2
            self._s = self._pa.open(format=pa.paInt16, channels=self.channels,
                                    rate=self.rate, input=True,
                                    input_device_index=d["index"],
                                    frames_per_buffer=int(self.rate * 0.1))
        except Exception:
            self._pa.terminate()
            raise

    def available(self) -> int:
        return self._s.get_read_available()

    def read(self, frames: int) -> bytes:
        return self._s.read(frames, exception_on_overflow=False)

    def close(self) -> None:
        try:
            self._s.stop_stream()
            self._s.close()
        finally:
            self._pa.terminate()


def open_loopback():
    """A seam: tests replace it with a synthetic source. Raises when there is
    no loopback to open (not Windows, PyAudioWPatch missing, no output)."""
    return _WasapiLoopback()


def pc_sound_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import pyaudiowpatch  # noqa: F401
        return True
    except Exception:      # noqa: BLE001
        return False


class Feeder(threading.Thread):
    """Loopback PCM → the write end of a pipe, kept level with the wall clock."""

    def __init__(self, source, fd: int):
        super().__init__(daemon=True, name="meeting-loopback")
        self.source, self.fd = source, fd
        self.bytes_per_frame = 2 * source.channels
        self._halt = threading.Event()
        self.error: str | None = None

    def stop(self, timeout: float = 5) -> None:
        self._halt.set()
        self.join(timeout)

    def run(self) -> None:
        rate, written, t0 = self.source.rate, 0, time.monotonic()
        try:
            while not self._halt.is_set():
                n = self.source.available()
                if n:
                    data = self.source.read(n)
                    os.write(self.fd, data)
                    written += len(data) // self.bytes_per_frame
                due = int((time.monotonic() - t0) * rate)
                if due - written > rate * _PAD_BEHIND_S:
                    # Nothing playing: WASAPI sent nothing. Silence, so this
                    # track keeps time with the microphone's.
                    gap = due - written
                    os.write(self.fd, b"\0" * gap * self.bytes_per_frame)
                    written += gap
                self._halt.wait(_FEED_TICK_S)
        except OSError as exc:            # ffmpeg went away
            self.error = str(exc)
        except Exception as exc:          # noqa: BLE001
            self.error = str(exc)
            log.warning("[MEETING] loopback feed stopped: %s", exc)
        finally:
            try:
                os.close(self.fd)         # EOF: ffmpeg finishes the file
            except OSError:
                pass
            try:
                self.source.close()
            except Exception:             # noqa: BLE001
                pass


# ── which windows are a call ─────────────────────────────────────────────────

_BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}
_TEAMS = {"ms-teams.exe", "teams.exe"}
_TEAMS_MAIN = re.compile(r"^(chat|activity|atividade|calendar|calendário|teams|equipas|calls|chamadas|"
                         r"files|ficheiros|apps|aplicações)\b", re.I)
_TEAMS_CALL = re.compile(r"\b(meeting|reunião|call|chamada)\b", re.I)
_MEET = re.compile(r"^(google\s+)?meet\s*[-–—:]|\bgoogle meet\b", re.I)
_ZOOM = re.compile(r"^zoom (meeting|webinar)|^reunião zoom|^zoom$", re.I)

BROWSER_GRACE_S = 180          # a tab switch hides the title; the call is not over
APP_GRACE_S = 30


def _windows() -> list[dict]:
    """Visible top-level windows as {app, title}. A seam for tests."""
    if sys.platform != "win32":
        return []
    from .desktop import _windows_win32
    return _windows_win32(())[1]


def call_windows(title_hint: str = "") -> list[dict]:
    """Windows that look like a call in progress: {app, title, browser}."""
    hint = (title_hint or "").strip().lower()
    out = []
    for w in _windows():
        app, title = (w.get("app") or "").lower(), w.get("title") or ""
        browser = app in _BROWSERS
        if browser and _MEET.search(title):
            pass
        elif app == "zoom.exe" and _ZOOM.search(title):
            pass
        elif app in _TEAMS and not _TEAMS_MAIN.search(title) and (
                _TEAMS_CALL.search(title) or (len(hint) >= 4 and hint in title.lower())):
            pass
        else:
            continue
        out.append({"app": app, "title": title, "browser": browser})
    return out


class EndWatch:
    """Calls `on_end` once the call windows seen at the start are all gone."""

    def __init__(self, anchors: list[dict]):
        self.anchors = anchors
        self.grace = BROWSER_GRACE_S if any(a["browser"] for a in anchors) else APP_GRACE_S
        self._missing_since: float | None = None

    def ended(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        titles = {w.get("title") for w in _windows()}
        if any(a["title"] in titles for a in self.anchors):
            self._missing_since = None
            return False
        if self._missing_since is None:
            self._missing_since = now
        return now - self._missing_since >= self.grace
