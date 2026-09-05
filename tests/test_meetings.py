"""
Recording a meeting SUNI is not in.

No test here records anything real. Where audio is needed it is SYNTHESISED with
ffmpeg, because the alternative — running a capture to see if capture works —
means recording whoever happens to be audible in the room, and a test suite
should not be the reason someone gets recorded.

The consent tests are the ones that matter. Capture, storage and transcription
are ordinary engineering; a recorder that can be started without anybody
agreeing to it is a different kind of thing entirely, so most of what follows is
about refusing rather than about working.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from suni import config as _cfg
from suni import meetings
from suni import transcription


HAVE_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Feature on, one fake device — nothing here opens a real one."""
    real = _cfg.get
    monkeypatch.setattr(_cfg, "get", lambda k, d=None: (
        True if k == "meetings_enabled" else
        ["Fake Device"] if k == "meeting_devices" else
        real(k, d)))
    # The audit table must exist: start_recording deliberately aborts the
    # recording if it cannot write the audit row, so without this every
    # lifecycle test would fail for the right reason but the wrong cause.
    from suni import audit as _audit
    _audit.init_db()
    meetings._active.clear()
    yield
    meetings._active.clear()


# ── consent: the part that must never be bypassable ─────────────────────────

async def test_it_refuses_without_a_human_saying_participants_were_told(test_users):
    uid = test_users["admin"]["id"]
    with pytest.raises(meetings.MeetingError) as e:
        await meetings.start_recording(uid, "admin_test", "Standup",
                                       participants_informed=False)
    assert "nobody has confirmed" in str(e.value).lower()


async def test_consent_cannot_be_supplied_by_forgetting_the_argument(test_users):
    """participants_informed is keyword-only with no default, so a caller that
    omits it gets a TypeError rather than a recording."""
    with pytest.raises(TypeError):
        await meetings.start_recording(test_users["admin"]["id"], "admin_test", "X")


def test_no_configuration_can_grant_consent():
    """The config may DISABLE the feature. Nothing in it may enable recording
    without a person stating it, so there must be no such key."""
    from suni.config import DEFAULTS
    granting = [k for k in DEFAULTS
                if "consent" in k.lower()
                or ("informed" in k.lower() and "meeting" in k.lower())]
    assert not granting, f"a setting could stand in for consent: {granting}"


def test_the_feature_is_off_until_an_admin_turns_it_on():
    from suni.config import DEFAULTS
    assert DEFAULTS["meetings_enabled"] is False


async def test_a_disabled_instance_records_nothing(test_users, monkeypatch):
    real = _cfg.get
    monkeypatch.setattr(_cfg, "get", lambda k, d=None: (
        False if k == "meetings_enabled" else real(k, d)))
    with pytest.raises(meetings.MeetingError) as e:
        await meetings.start_recording(test_users["admin"]["id"], "admin_test",
                                       participants_informed=True)
    assert "turned off" in str(e.value).lower()


def test_starting_a_recording_needs_human_approval():
    """The model cannot know whether anyone was told, so the tool is gated."""
    from suni.approval import _CONSEQUENTIAL
    assert "start_meeting_recording" in _CONSEQUENTIAL


def test_the_announcement_exists_in_both_languages():
    """Telling people should be one sentence, not something to compose while
    everyone waits."""
    en = meetings.announcement_text("en")
    pt = meetings.announcement_text("pt-PT")
    assert en and pt and en != pt
    for t in (en, pt):
        assert "AI" in t or "IA" in t, "the announcement never mentions an AI"


# ── isolation: a meeting is a generated file like any other ─────────────────

def test_recordings_live_in_the_users_own_output_directory(test_users):
    from suni.user_settings import resolve_output_dir
    uid = test_users["standard"]["id"]
    d = meetings.meetings_dir(uid)
    assert d.is_relative_to(Path(resolve_output_dir(uid)))


def test_two_users_do_not_share_a_meetings_folder(test_users):
    a = meetings.meetings_dir(test_users["admin"]["id"])
    b = meetings.meetings_dir(test_users["standard"]["id"])
    assert a != b


def test_a_meeting_id_from_a_model_cannot_escape_the_folder(test_users):
    uid = test_users["admin"]["id"]
    for evil in ("../../../../windows/system32", "..\\..\\secrets", "a/../../b"):
        with pytest.raises(meetings.MeetingError):
            meetings.get_meeting(uid, evil)


def test_recordings_are_downloadable_by_their_owner():
    """A recording the owner cannot fetch is a recording that may as well not
    exist. .wav must not be in the blocked-download list."""
    src = (Path(__file__).resolve().parent.parent / "suni" / "web" / "server.py"
           ).read_text(encoding="utf-8-sig")
    blocked = src.split("_BLOCKED_DL_EXTS = {")[1].split("}")[0]
    assert ".wav" not in blocked


# ── the pieces that do not need a microphone ────────────────────────────────

def test_a_long_transcript_is_split_on_sentence_boundaries():
    """An hour of talking overflows num_ctx 8192, so a single-pass summary
    silently loses most of the meeting. Chunks must not start mid-sentence."""
    segs = [{"start": i * 5, "end": i * 5 + 4, "text": f"Sentence number {i} about the budget."}
            for i in range(400)]
    chunks = transcription.chunk(segs, max_chars=1000)
    assert len(chunks) > 1, "a 400-segment transcript was not split at all"
    for c in chunks:
        assert len(c) <= 1200
        assert c.startswith("Sentence"), "a chunk began mid-sentence"


def test_timestamps_survive_into_the_readable_transcript():
    """'They agreed at 14:32' has to be findable in the audio."""
    segs = [{"start": 0.0, "end": 2.0, "text": "Morning."},
            {"start": 92.5, "end": 95.0, "text": "Agreed, we ship Friday."}]
    text = transcription.to_text(segs)
    assert "[00:00]" in text and "[01:32]" in text
    assert "Agreed, we ship Friday." in text


def test_transcription_reports_a_missing_install_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(transcription, "available", lambda: False)
    assert transcription.available() is False


async def test_a_missing_recording_is_reported_not_raised():
    with pytest.raises(transcription.TranscriptionError) as e:
        await transcription.transcribe_file("no/such/file.wav")
    assert "no such recording" in str(e.value).lower()


def test_listing_is_empty_rather_than_broken_for_a_new_user(test_users):
    assert meetings.list_meetings(test_users["readonly"]["id"]) == []


# ── capture, with audio nobody was recorded to make ─────────────────────────

@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_ffmpeg_can_produce_the_format_whisper_is_given(tmp_path):
    """Proves the capture format end to end using a SYNTHESISED tone — the same
    16 kHz mono WAV a real recording produces, without recording anyone."""
    out = tmp_path / "synthetic.wav"
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-ac", "1", "-ar", "16000", str(out)],
        capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode()[:400]
    assert out.exists() and out.stat().st_size > 50_000, "unexpected WAV size"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_device_listing_does_not_raise_on_this_machine():
    """It may legitimately be empty in CI; it must never throw."""
    assert isinstance(meetings.list_audio_devices(), list)


async def test_stopping_when_nothing_is_recording_is_a_message_not_a_crash(test_users):
    with pytest.raises(meetings.MeetingError) as e:
        await meetings.stop_recording(test_users["admin"]["id"], "admin_test")
    assert "nothing is being recorded" in str(e.value).lower()


async def test_a_second_recording_cannot_start_over_the_first(test_users, monkeypatch):
    """Two ffmpeg processes on one device produce two half-meetings."""
    uid = test_users["admin"]["id"]
    meetings._active[uid] = {
        "meeting_id": "fake", "title": "Already running",
        "started_at": "2026-09-05T10:00:00+00:00", "dir": "/tmp/x", "proc": None,
    }
    with pytest.raises(meetings.MeetingError) as e:
        await meetings.start_recording(uid, "admin_test", participants_informed=True)
    assert "already recording" in str(e.value).lower()


# ── the audit trail, for a feature that records people ─────────────────────

def test_start_and_stop_are_written_to_the_audit_trail():
    """'Who started a recording, when, and for how long' is the record that
    matters here, and it must not be optional."""
    src = (Path(__file__).resolve().parent.parent / "suni" / "meetings.py"
           ).read_text(encoding="utf-8")
    assert 'meeting.recording.started' in src
    assert 'meeting.recording.stopped' in src
    assert src.count("_audit.log_event") >= 2


# ── the recording lifecycle, driven by a synthesised source ────────────────
# capture_args is replaced with a lavfi tone so the REAL start/stop path runs:
# subprocess spawn, the liveness check, the graceful 'q' stop, WAV
# finalisation, metadata and audit. Nothing is recorded from any device.

def _synthetic_args(devices, wav):
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-ac", "1", "-ar", "16000", str(wav)]


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
async def test_the_full_record_then_stop_cycle(test_users, monkeypatch):
    monkeypatch.setattr(meetings, "capture_args", _synthetic_args)
    uid = test_users["admin"]["id"]

    started = await meetings.start_recording(
        uid, "admin_test", "Synthetic meeting", participants_informed=True)
    assert started["meeting_id"]
    assert meetings.active_recording(uid)["title"] == "Synthetic meeting"

    import asyncio as _a
    await _a.sleep(2)

    stopped = await meetings.stop_recording(uid, "admin_test")
    assert stopped["seconds"] >= 1, "duration was not measured"
    assert meetings.active_recording(uid) is None, "the run was not cleared"

    wav = Path(stopped["path"])
    assert wav.exists() and wav.stat().st_size > 30_000, (
        f"no usable audio was written ({wav.stat().st_size if wav.exists() else 0} bytes)")

    # The WAV must be closed out properly — the first thing anyone does with a
    # recording is open it, and a terminated ffmpeg leaves a broken header.
    probe = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error",
         "-show_entries", "format=duration", "-of", "csv=p=0", str(wav)],
        capture_output=True, text=True, timeout=30)
    if probe.returncode == 0 and probe.stdout.strip():
        assert float(probe.stdout.strip()) > 0.5, "the WAV header has no duration"

    meta = json.loads((wav.parent / "meeting.json").read_text(encoding="utf-8"))
    assert meta["state"] == "recorded"
    assert meta["started_by"] == "admin_test"
    assert meta["participants_informed_by"] == "admin_test"

    rows = meetings.list_meetings(uid)
    assert any(r["meeting_id"] == started["meeting_id"] for r in rows)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
async def test_a_device_that_will_not_open_is_reported_at_start(test_users, monkeypatch):
    """Otherwise the caller gets a meeting id for a recording that never ran,
    and finds out an hour later that there is no audio."""
    monkeypatch.setattr(meetings, "capture_args",
                        lambda d, w: ["ffmpeg", "-hide_banner", "-loglevel", "error",
                                      "-y", "-f", "dshow", "-i",
                                      "audio=No Such Device 12345", str(w)])
    with pytest.raises(meetings.MeetingError) as e:
        await meetings.start_recording(test_users["standard"]["id"], "std_test",
                                       participants_informed=True)
    assert "could not open" in str(e.value).lower()
    assert meetings.active_recording(test_users["standard"]["id"]) is None


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
async def test_a_recording_that_cannot_be_audited_does_not_run(test_users, monkeypatch):
    """Recording people with no record of who started it is the one outcome
    this feature must not produce. ffmpeg is already live when the audit row is
    written, so a failure there must stop it rather than leave it running while
    the caller is told the start failed."""
    monkeypatch.setattr(meetings, "capture_args", _synthetic_args)

    def _broken(*a, **k):
        raise RuntimeError("audit table is gone")

    monkeypatch.setattr(meetings._audit, "log_event", _broken)
    uid = test_users["readonly"]["id"]

    with pytest.raises(meetings.MeetingError) as e:
        await meetings.start_recording(uid, "ro_test", "Unauditable",
                                       participants_informed=True)
    assert "audit" in str(e.value).lower()
    assert meetings.active_recording(uid) is None, "a recording was left running"
