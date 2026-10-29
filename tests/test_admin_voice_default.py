"""The admin panel's TTS voice is the default for anyone who has not chosen one.

It used to reach nothing: /api/tts fell back to TTS_VOICE, read from the
SUNI_TTS_VOICE environment variable at import, so the panel saved a voice that
was never spoken. The language rule it sits beside must survive the fix — an
English default must not read a Portuguese speaker's replies.
"""
from __future__ import annotations

import pytest

from suni import config as suni_config
from suni.web import server as S


@pytest.fixture
def admin_voice(monkeypatch):
    def _set(v):
        monkeypatch.setitem(suni_config._cache, "tts_voice", v)
    return _set


def test_untouched_default_defers_to_the_environment(admin_voice, monkeypatch):
    admin_voice(suni_config.DEFAULTS["tts_voice"])
    monkeypatch.setattr(S, "TTS_VOICE", "en-GB-RyanNeural")
    assert S._admin_voice() == "en-GB-RyanNeural"


def test_a_chosen_admin_voice_is_used(admin_voice):
    admin_voice("en-GB-LibbyNeural")
    assert S._default_voice_for("") == "en-GB-LibbyNeural"
    assert S._default_voice_for("en-US") == "en-GB-LibbyNeural"


def test_portuguese_speaker_keeps_a_portuguese_voice_under_an_english_default(admin_voice):
    # The negative case: the reason the language rule exists.
    admin_voice("en-GB-SoniaNeural")
    assert S._default_voice_for("pt-PT") == "pt-PT-RaquelNeural"


def test_admin_portuguese_voice_wins_for_portuguese_speakers(admin_voice):
    admin_voice("pt-PT-DuarteNeural")
    assert S._default_voice_for("pt-PT") == "pt-PT-DuarteNeural"


def test_english_speaker_is_not_given_a_portuguese_default(admin_voice, monkeypatch):
    admin_voice("pt-PT-DuarteNeural")
    monkeypatch.setattr(S, "TTS_VOICE", "en-GB-SoniaNeural")
    assert S._default_voice_for("en-GB") == "en-GB-SoniaNeural"


def test_an_invalid_admin_voice_cannot_silence_speech(admin_voice):
    admin_voice("xx-XX-NobodyNeural")
    assert S._admin_voice() == S.TTS_VOICE


def test_tts_route_uses_the_default_not_the_constant(client, std_headers, monkeypatch):
    """Someone with no voice of their own hears the admin's default."""
    monkeypatch.setitem(suni_config._cache, "tts_backend", "edge")
    monkeypatch.setitem(suni_config._cache, "tts_voice", "en-GB-LibbyNeural")
    monkeypatch.setattr(S._user_settings, "get", lambda uid: {"response_language": "en-GB"})
    monkeypatch.setattr(S, "TTS_VOICE", "en-GB-RyanNeural")
    r = client.get("/api/tts/plan", headers=std_headers)
    assert r.json() == {**r.json(), "provider": "microsoft", "voice": "en-GB-LibbyNeural"}
