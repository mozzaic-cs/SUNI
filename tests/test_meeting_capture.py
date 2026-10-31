"""The PC's own sound and the automatic stop (suni/meeting_capture.py).

Nothing here opens a real device or reads the real desktop: the loopback is a
synthetic tone, the window list is a fixture. Same rule as test_meetings.py — a
test suite must not be the reason someone gets recorded.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest

from suni import config as _cfg
from suni import meeting_capture as cap
from suni import meetings

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


class FakeLoopback:
    """A 440 Hz tone that arrives like WASAPI: in bursts, and not at all while
    `silent` is set (nothing playing)."""
    name = "Fake Speakers [Loopback]"
    rate, channels = 48000, 2

    def __init__(self):
        self.silent = False
        self.closed = False
        self._t0 = time.monotonic()
        self._given = 0

    def available(self):
        due = int((time.monotonic() - self._t0) * self.rate)
        n = due - self._given
        if self.silent:
            self._given = due          # time passes, WASAPI sends nothing
            return 0
        return n

    def read(self, frames):
        out = bytearray()
        for i in range(frames):
            v = int(8000 * math.sin(2 * math.pi * 440 * (self._given + i) / self.rate))
            out += struct.pack("<hh", v, v)
        self._given += frames
        return bytes(out)

    def close(self):
        self.closed = True


WINDOWS: list[dict] = []


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    real = _cfg.get
    values = {"meetings_enabled": True, "meeting_devices": [cap.PC_SOUND],
              "meeting_auto_stop": True, "meeting_max_minutes": 240}
    monkeypatch.setattr(_cfg, "get", lambda k, d=None: values[k] if k in values else real(k, d))
    monkeypatch.setattr(cap, "_windows", lambda: list(WINDOWS))
    from suni import audit as _audit
    _audit.init_db()
    WINDOWS.clear()
    meetings._active.clear()
    yield values
    meetings._active.clear()
    WINDOWS.clear()


def _duration(wav: Path) -> float:
    r = subprocess.run(["ffprobe", "-hide_banner", "-loglevel", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(wav)],
                       capture_output=True, text=True, timeout=30)
    return float(r.stdout.strip() or 0)


# ── the feed ─────────────────────────────────────────────────────────────────

def test_silence_is_written_while_nothing_plays():
    """Without padding a pause in the call shortens the PC track, and it drifts
    away from the microphone's by every pause."""
    src = FakeLoopback()
    src.silent = True
    r, w = os.pipe()
    got = bytearray()
    def drain():
        while chunk := os.read(r, 65536):
            got.extend(chunk)
    reader = threading.Thread(target=drain)
    reader.start()
    f = cap.Feeder(src, w)
    f.start()
    time.sleep(1.0)
    f.stop()
    reader.join(5)
    os.close(r)
    seconds = len(got) / (src.rate * 4)
    assert 0.6 < seconds < 1.4, seconds
    assert not any(got), "silence must be zeros"
    assert src.closed, "the device was left open"


def test_the_ffmpeg_command_puts_the_pc_sound_first_and_ends_with_it():
    args = meetings.capture_args(["Microphone (USB)"], Path("x.wav"), loopback=(48000, 2))
    line = " ".join(args)
    assert line.index("pipe:0") < line.index("audio=Microphone (USB)")
    assert "amix=inputs=2:duration=first" in line
    assert "-ar 48000 -ac 2" in line


# ── a real ffmpeg, a synthetic PC sound ──────────────────────────────────────

@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
async def test_the_pc_sound_is_recorded_and_stops_cleanly(test_users, monkeypatch):
    src = FakeLoopback()
    monkeypatch.setattr(cap, "open_loopback", lambda: src)
    uid = test_users["admin"]["id"]

    r = await meetings.start_recording(uid, "admin_test", "PC sound", participants_informed=True)
    assert r["sources"] == ["PC sound (Fake Speakers [Loopback])"]
    await asyncio.sleep(1.5)
    src.silent = True                   # a pause in the call
    await asyncio.sleep(1.5)
    stopped = await meetings.stop_recording(uid, "admin_test")

    wav = Path(stopped["path"])
    d = _duration(wav)
    # 1.5 s start check + 3 s; the pause must be in the file, not dropped.
    assert d >= 3.5, f"only {d:.1f}s recorded: the silent stretch was lost"
    assert src.closed
    assert meetings.active_recording(uid) is None


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
async def test_no_loopback_is_said_at_start(test_users, monkeypatch):
    def boom():
        raise OSError("no default output device")
    monkeypatch.setattr(cap, "open_loopback", boom)
    with pytest.raises(meetings.MeetingError) as e:
        await meetings.start_recording(test_users["standard"]["id"], "std_test",
                                       participants_informed=True)
    assert "PC's sound" in str(e.value)


# ── which windows are a call ─────────────────────────────────────────────────

@pytest.mark.parametrize("app,title,is_call", [
    ("chrome.exe", "Meet - abc-defg-hij", True),
    ("msedge.exe", "Meet – xyz-abcd-efg – Microsoft Edge", True),
    ("chrome.exe", "Gmail - Inbox", False),
    ("Zoom.exe", "Zoom Meeting", True),
    ("Zoom.exe", "Zoom Workplace", False),
    ("ms-teams.exe", "Meeting with Ana | Microsoft Teams", True),
    ("ms-teams.exe", "Chat | Ana | Microsoft Teams", False),
    ("ms-teams.exe", "Calendar | Microsoft Teams", False),
    ("notepad.exe", "Meet - notes.txt", False),
])
def test_call_windows(app, title, is_call):
    WINDOWS[:] = [{"app": app, "title": title}]
    assert bool(cap.call_windows()) is is_call


def test_a_teams_window_named_after_the_meeting_counts():
    """How the September script found its call: the meeting's own name."""
    WINDOWS[:] = [{"app": "ms-teams.exe", "title": "Quarterly Platform Review | Microsoft Teams"}]
    assert cap.call_windows("Platform Review")
    assert not cap.call_windows("")


def test_the_end_waits_out_the_grace_and_a_tab_switch_waits_longer():
    WINDOWS[:] = [{"app": "zoom.exe", "title": "Zoom Meeting"}]
    w = cap.EndWatch(cap.call_windows())
    WINDOWS.clear()
    assert not w.ended(now=100)
    assert not w.ended(now=100 + cap.APP_GRACE_S - 1)
    assert w.ended(now=100 + cap.APP_GRACE_S)

    WINDOWS[:] = [{"app": "chrome.exe", "title": "Meet - abc-defg-hij"}]
    b = cap.EndWatch(cap.call_windows())
    WINDOWS.clear()
    assert not b.ended(now=0)
    assert not b.ended(now=cap.APP_GRACE_S + 1), "a tab switch is not the end of the call"
    WINDOWS[:] = [{"app": "chrome.exe", "title": "Meet - abc-defg-hij"}]
    assert not b.ended(now=cap.APP_GRACE_S + 2), "back on the tab: the clock restarts"
    WINDOWS.clear()
    assert not b.ended(now=cap.APP_GRACE_S + 3)
    assert b.ended(now=cap.APP_GRACE_S + 3 + cap.BROWSER_GRACE_S)


# ── the automatic stop, end to end ───────────────────────────────────────────

@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
async def test_closing_the_call_window_stops_the_recording(test_users, monkeypatch):
    src = FakeLoopback()
    monkeypatch.setattr(cap, "open_loopback", lambda: src)
    monkeypatch.setattr(meetings, "_WATCH_TICK_S", 0.2)
    monkeypatch.setattr(cap, "APP_GRACE_S", 0.5)
    after = []

    async def hook(uid, stopped):
        after.append((uid, stopped["meeting_id"]))
    monkeypatch.setattr(meetings, "on_auto_stop", hook)
    WINDOWS[:] = [{"app": "zoom.exe", "title": "Zoom Meeting"}]
    uid = test_users["admin"]["id"]

    r = await meetings.start_recording(uid, "admin_test", "Zoom", participants_informed=True)
    assert r["auto_stop"] == ["Zoom Meeting"]
    await asyncio.sleep(0.5)
    WINDOWS.clear()                     # the call ends
    for _ in range(50):
        if meetings.active_recording(uid) is None and after:
            break
        await asyncio.sleep(0.1)
    assert meetings.active_recording(uid) is None, "still recording after the call closed"
    assert after == [(uid, r["meeting_id"])], "the transcript was not started"
    mdir, meta = meetings.get_meeting(uid, r["meeting_id"])
    assert meta["stopped"] == "the call window closed"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
async def test_nothing_runs_past_the_length_cap(test_users, monkeypatch, _setup):
    src = FakeLoopback()
    monkeypatch.setattr(cap, "open_loopback", lambda: src)
    monkeypatch.setattr(meetings, "_WATCH_TICK_S", 0.2)
    monkeypatch.setattr(meetings, "on_auto_stop", None)
    _setup["meeting_max_minutes"] = 2.5 / 60          # 2.5 s
    uid = test_users["standard"]["id"]

    r = await meetings.start_recording(uid, "std_test", "Forgotten", participants_informed=True)
    assert r["auto_stop"] == [], "no call window: no window-based stop"
    for _ in range(80):
        _, meta = meetings.get_meeting(uid, r["meeting_id"])
        if "stopped" in meta:           # written once ffmpeg has closed the file
            break
        await asyncio.sleep(0.1)
    assert meetings.active_recording(uid) is None
    assert meta.get("stopped", "").startswith("it reached"), meta


async def test_a_manual_stop_ends_the_watch(test_users, monkeypatch, tmp_path):
    """Stopping by hand must not leave a watcher that later stops the NEXT
    recording."""
    class P:
        returncode = 0
        async def wait(self): return 0
    monkeypatch.setattr(meetings, "meetings_dir", lambda uid: tmp_path)
    uid = test_users["readonly"]["id"]
    mdir = meetings._meeting_dir(uid, "m1")
    (mdir / "audio.wav").write_bytes(b"\0" * 5000)
    meetings._write_meta(mdir, {"meeting_id": "m1", "title": "t"})
    watch = asyncio.create_task(asyncio.sleep(60))
    meetings._active[uid] = {"meeting_id": "m1", "title": "t", "proc": P(), "dir": str(mdir),
                             "started_at": "2026-10-31T10:00:00+00:00", "feeder": None,
                             "watch": watch}
    await meetings.stop_recording(uid, "ro_test")
    await asyncio.sleep(0)
    assert watch.cancelled()


# ── one transcription per meeting ────────────────────────────────────────────

async def test_asking_while_it_transcribes_waits_for_the_same_job(monkeypatch):
    from suni.tools import meeting_tool as mt
    runs = []

    async def slow(uid, mid):
        runs.append(mid)
        await asyncio.sleep(0.3)
        return "Transcript"
    monkeypatch.setattr(mt, "_transcribe", slow)
    a, b = await asyncio.gather(mt.transcribe_for("u1", "m1"), mt.transcribe_for("u1", "m1"))
    assert a == b == "Transcript"
    assert runs == ["m1"]


async def test_a_cancelled_ask_does_not_cancel_the_transcription(monkeypatch):
    """A Claude Code turn that times out cancels its tool call; the work must
    carry on so the next ask finds it done."""
    from suni.tools import meeting_tool as mt
    done = []

    async def slow(uid, mid):
        await asyncio.sleep(0.3)
        done.append(mid)
        return "Transcript"
    monkeypatch.setattr(mt, "_transcribe", slow)
    ask = asyncio.create_task(mt.transcribe_for("u2", "m2"))
    await asyncio.sleep(0.05)
    ask.cancel()
    await asyncio.sleep(0.5)
    assert done == ["m2"]
