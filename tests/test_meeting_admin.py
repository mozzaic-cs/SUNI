"""Meeting recording can be switched on from the admin panel.

Until 2026-10-31 meetings_enabled and meeting_devices existed only in the
config file: nothing in the admin panel showed them, and the device list a
config comment promised (GET /api/meetings/devices) did not exist.
"""
from __future__ import annotations

import pathlib

from suni import meetings

HTML = (pathlib.Path(__file__).resolve().parent.parent
        / "suni" / "web" / "admin.html").read_text(encoding="utf-8")


def test_the_device_list_is_for_admins_only(client, admin_headers, std_headers, monkeypatch):
    monkeypatch.setattr(meetings, "list_audio_devices",
                        lambda: ["Stereo Mix (Realtek High Definition Audio)",
                                 "Microphone (USB Audio CODEC )"])
    r = client.get("/api/meetings/devices", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["devices"] == ["Stereo Mix (Realtek High Definition Audio)",
                                   "Microphone (USB Audio CODEC )"]
    assert client.get("/api/meetings/devices", headers=std_headers).status_code == 403


def test_no_ffmpeg_is_said_not_raised(client, admin_headers, monkeypatch):
    def boom():
        raise meetings.MeetingError("ffmpeg is not installed or not on PATH.")
    monkeypatch.setattr(meetings, "list_audio_devices", boom)
    r = client.get("/api/meetings/devices", headers=admin_headers)
    assert r.status_code == 200
    assert r.json() == {"devices": [], "error": "ffmpeg is not installed or not on PATH."}


def test_the_form_has_the_switch_and_saves_it():
    assert 'name="meetings_enabled"' in HTML
    assert "data.meetings_enabled" in HTML
    assert "data.meeting_whisper_model" in HTML


def test_saving_before_the_device_list_loads_keeps_the_stored_devices():
    # Before the list renders the picker is empty; posting [] then would wipe
    # the configured devices on an unrelated save.
    assert "if (_meetingDevicesLoaded) data.meeting_devices = _meetingDevices();" in HTML
