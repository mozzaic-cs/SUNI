"""Speech can be stopped mid-sentence and picked up again.

The 🔊 toggle only decided whether the NEXT reply would be spoken. Once SUNI
started talking there was no way to stop it short of sending something else,
which throws the rest of the answer away — and on the Face, which is meant to be
left running in a room, that is the difference between an assistant and a
nuisance.

Pause is not the same control as the run-stop button: that one halts the model,
this one halts the voice while the answer stands.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
UIS = ("suni/web/face.html", "suni/web/ui.html", "suni/web/chat.html")

# Anchored on the button's own variable: these pages have several click
# handlers, and a looser pattern cheerfully matches someone else's — it did,
# and the tests passed against code they were not testing.
_HANDLER = re.compile(r"_?ttsPauseBtn\.addEventListener\('click'.*?\n\}\);", re.S)
_SETTER = re.compile(r"function _ttsSetPaused\(on\).*?\n\}", re.S)


def src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def handler(rel: str) -> str:
    m = _HANDLER.search(src(rel))
    assert m, f"{rel}: the pause button has no click handler"
    return m.group(0)


@pytest.mark.parametrize("rel", UIS)
def test_every_interface_has_the_control(rel):
    assert 'id="tts-pause"' in src(rel), "no way to stop the voice on this page"


@pytest.mark.parametrize("rel", UIS)
def test_it_pauses_and_resumes_the_same_audio(rel):
    body = handler(rel)
    assert ".pause()" in body, "pausing does not pause the audio"
    assert ".play()" in body, "there is no way back — resume is the point"


@pytest.mark.parametrize("rel", UIS)
def test_resuming_is_offered_only_while_something_is_playing(rel):
    """A ▶ that resumes audio which finished long ago is a lie about state."""
    body = handler(rel)
    assert "ended" in body or "speaking" in body, \
        "the handler pauses whatever the element holds, played out or not"


@pytest.mark.parametrize("rel", UIS)
def test_a_new_reply_clears_the_paused_state(rel):
    """send() replaces the audio; a button still showing ▶ would offer to resume
    something that no longer exists."""
    s = src(rel)
    i = s.index("_ttsGen++")
    assert "_ttsSetPaused(false)" in s[i:i + 400], \
        "a fresh reply leaves the control stuck on resume"


@pytest.mark.parametrize("rel", UIS)
def test_the_label_follows_the_state(rel):
    m = _SETTER.search(src(rel))
    assert m, f"{rel}: nothing keeps the button's label in step"
    body = m.group(0)
    assert "▶" in body and "⏸" in body, "the button always shows the same icon"
    assert "orb.tts_resume" in body and "orb.tts_pause" in body, \
        "the tooltip is not translated"


def test_both_languages_have_the_words():
    i18n = src("suni/web/i18n.js")
    for key in ("orb.tts_pause", "orb.tts_resume"):
        assert i18n.count(f"'{key}'") == 2, \
            f"{key} is missing from a language — a missing key renders as the raw key"


@pytest.mark.parametrize("rel", UIS)
def test_the_run_stop_button_is_left_alone(rel):
    """Halting the voice must not be confused with halting the model: the AI Act
    Art 14(4)(e) control is a different button doing a different thing."""
    assert "/api/chat/stop" not in handler(rel)
