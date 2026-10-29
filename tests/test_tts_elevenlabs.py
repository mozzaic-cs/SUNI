"""ElevenLabs as the admin-chosen voice provider.

Two rules carry the weight here. Portuguese is spoken only by a European
Portuguese voice: the account's premade voices are English ones ElevenLabs
verified for pt-BR, which would read Portuguese with a Brazilian or American
accent. And when the month's credits run out, Microsoft speaks instead —
never silence, and never benching Microsoft along with it.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from suni import config as suni_config
from suni import tts_elevenlabs as EL


def _voice(vid, name, language="en", accent="american", verified=()):
    return NS(voice_id=vid, name=name, labels={"language": language, "accent": accent},
              verified_languages=[NS(language=l[:2], locale=l, accent="standard", model_id="m")
                                  for l in verified])


# What this account actually holds (2026-10-29): English voices, some verified pt-BR.
ROGER = _voice("roger", "Roger - Laid-Back, Casual")
CHARLIE = _voice("charlie", "Charlie - Deep", accent="australian", verified=("pt-BR",))
# A European Portuguese voice from the library, both ways it can be marked.
CLAUDIA = _voice("claudia", "Claudia - Friendly", language="pt", accent="european")
PAULO = _voice("paulo", "Paulo - Diplomatic", language="pt", accent="", verified=("pt-PT",))


@pytest.fixture
def account(monkeypatch):
    def _set(*voices):
        EL._voice_cache.update(at=10**12, voices=[EL._describe(v) for v in voices])
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test-key")
    saved = dict(EL._voice_cache)
    yield _set
    EL._voice_cache.clear()
    EL._voice_cache.update(saved)


def _pick(saved="", admin="", lang="pt-PT"):
    import asyncio
    return asyncio.run(EL.pick(saved, admin, lang))


# ── European Portuguese only ────────────────────────────────────────────────
def test_a_brazilian_verified_voice_never_speaks_portuguese(account):
    account(ROGER, CHARLIE)
    assert _pick(lang="pt-PT") is None, "a pt-BR voice was chosen for Portuguese"
    assert _pick(saved="eleven:charlie", lang="pt-PT") is None, "a saved pt-BR voice was honoured"


def test_bare_pt_also_means_european(account):
    account(CHARLIE, CLAUDIA)
    assert _pick(lang="pt") == "claudia"


@pytest.mark.parametrize("voice", [CLAUDIA, PAULO], ids=["accent-label", "verified-locale"])
def test_a_european_portuguese_voice_is_recognised(account, voice):
    account(ROGER, voice)
    assert _pick(lang="pt-PT") == voice.voice_id


def test_the_speakers_own_voice_wins_when_it_suits(account):
    account(CLAUDIA, PAULO)
    assert _pick(saved="eleven:paulo", admin="claudia") == "paulo"
    assert _pick(saved="pt-PT-RaquelNeural", admin="claudia") == "claudia", \
        "a Microsoft voice id leaked into ElevenLabs"


def test_english_speakers_get_an_english_voice(account):
    account(CLAUDIA, ROGER)
    assert _pick(admin="claudia", lang="en-GB") == "roger"


def test_labels_are_readable(account):
    account(CLAUDIA, ROGER)
    assert EL.label_for("eleven:claudia") == "Claudia (pt-PT) — ElevenLabs"
    assert EL.label_for("roger") == "Roger (en) — ElevenLabs"


# ── credits ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("exc,quota", [
    (NS(status_code=401, body={"detail": {"status": "quota_exceeded"}}), True),
    (NS(status_code=402, body=None), True),
    # Measured 2026-10-29 with a pt-PT library voice on the free plan.
    (NS(status_code=402, body={"detail": {"code": "paid_plan_required",
                                          "status": "payment_required"}}), True),
    (NS(status_code=401, body={"detail": {"status": "invalid_api_key"}}), False),
    (NS(status_code=500, body="boom"), False),
])
def test_spent_credits_are_told_apart_from_other_failures(exc, quota):
    assert EL._is_quota(exc) is quota


class _Convert:
    """text_to_speech.convert, as the SDK shapes it: an async iterator."""
    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    def __call__(self, voice_id, **kw):
        self.calls.append((voice_id, kw))
        fail = self.fail

        async def gen():
            if fail:
                import asyncio
                await asyncio.sleep(0.05)   # a network round trip: others get to run
                raise fail
            yield b"ID3"
            yield b"el-mp3"
        return gen()


def _sdk(monkeypatch, convert):
    monkeypatch.setattr(EL, "_client", lambda: NS(text_to_speech=NS(convert=convert)))


def _speaker(monkeypatch, S, lang="pt-PT", voice=""):
    monkeypatch.setitem(suni_config._cache, "tts_backend", "elevenlabs")
    monkeypatch.setattr(S._user_settings, "get",
                        lambda uid: {"response_language": lang, "tts_voice": voice})


def test_elevenlabs_speaks_when_chosen(client, app, std_headers, monkeypatch, account):
    from suni.web import server as S
    account(ROGER, CLAUDIA)
    conv = _Convert()
    _sdk(monkeypatch, conv)
    _speaker(monkeypatch, S)
    app.state.eleven_tts["down_until"] = 0.0
    r = client.post("/api/tts", json={"text": "Olá."}, headers=std_headers)
    assert r.status_code == 200 and r.content == b"ID3el-mp3"
    assert r.headers["content-type"] == "audio/mpeg"
    vid, kw = conv.calls[0]
    assert vid == "claudia" and kw["text"] == "Olá." and kw["language_code"] == "pt"
    assert client.get("/api/tts/plan", headers=std_headers).json()["label"] == \
        "Claudia (pt-PT) — ElevenLabs"


class _QuotaError(Exception):
    status_code = 401
    body = {"detail": {"status": "quota_exceeded", "message": "el-test-key"}}


def _microsoft(monkeypatch, S, heard):
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)

    class Edge:
        def __init__(self, text, voice):
            heard.append(voice)

        async def stream(self):
            yield {"type": "audio", "data": b"edge-mp3"}
    monkeypatch.setattr(S.edge_tts, "Communicate", Edge)


def test_spent_credits_fall_back_to_microsoft_and_rest(client, app, std_headers,
                                                      monkeypatch, account, caplog):
    from suni.web import server as S
    account(CLAUDIA)
    conv = _Convert(fail=_QuotaError())
    _sdk(monkeypatch, conv)
    _speaker(monkeypatch, S)
    heard = []
    _microsoft(monkeypatch, S, heard)
    app.state.eleven_tts["down_until"] = 0.0
    app.state.cloud_tts["down_until"] = 0.0
    try:
        for text in ("Um.", "Dois."):
            r = client.post("/api/tts", json={"text": text}, headers=std_headers)
            assert r.status_code == 200 and r.content == b"edge-mp3"
        assert heard == ["pt-PT-RaquelNeural"] * 2, "not Microsoft's pt-PT voice"
        assert len(conv.calls) == 1, "spent credits retried before every sentence"
        assert app.state.cloud_tts["down_until"] == 0.0, "Microsoft benched with it"
        assert "el-test-key" not in caplog.text
    finally:
        app.state.eleven_tts["down_until"] = 0.0


def test_no_european_voice_means_microsoft_not_an_accent(client, app, std_headers,
                                                         monkeypatch, account):
    from suni.web import server as S
    account(ROGER, CHARLIE)
    conv = _Convert()
    _sdk(monkeypatch, conv)
    _speaker(monkeypatch, S)
    heard = []
    _microsoft(monkeypatch, S, heard)
    app.state.eleven_tts["down_until"] = 0.0
    app.state.cloud_tts["down_until"] = 0.0
    r = client.post("/api/tts", json={"text": "Olá."}, headers=std_headers)
    assert r.content == b"edge-mp3" and heard == ["pt-PT-RaquelNeural"]
    assert conv.calls == []


def test_without_a_key_it_is_microsoft(client, std_headers, monkeypatch, account):
    from suni.web import server as S
    account(CLAUDIA)
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    _speaker(monkeypatch, S)
    assert client.get("/api/tts/plan", headers=std_headers).json()["provider"] == "microsoft"


def test_the_voice_menu_follows_the_provider(client, std_headers, monkeypatch, account):
    account(CLAUDIA)
    monkeypatch.setitem(suni_config._cache, "tts_backend", "elevenlabs")
    d = client.get("/api/tts/voices", headers=std_headers).json()
    assert d["voices"] == ["eleven:claudia"]
    assert d["labels"]["eleven:claudia"].startswith("Claudia")
    d = client.get("/api/tts/voices?provider=microsoft", headers=std_headers).json()
    assert "pt-PT-RaquelNeural" in d["voices"]


def test_credits_are_for_admins(client, std_headers):
    assert client.get("/api/tts/elevenlabs/usage", headers=std_headers).status_code == 403


def test_the_key_is_never_in_the_code_or_config():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    src = (root / "suni" / "tts_elevenlabs.py").read_text(encoding="utf-8")
    assert 'os.environ.get("ELEVENLABS_API_KEY"' in src
    assert "elevenlabs_api_key" not in suni_config.DEFAULTS


def test_an_unreachable_voice_list_is_not_asked_before_every_sentence(monkeypatch):
    """Every /api/tts asks for the list. Unreachable, it cost up to the full
    timeout per sentence before Microsoft spoke."""
    import asyncio
    calls = []

    async def get_all(**kw):
        calls.append(kw)
        raise TimeoutError
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test-key")
    monkeypatch.setattr(EL, "_client", lambda: NS(voices=NS(get_all=get_all)))
    monkeypatch.setattr(EL, "_voice_cache", {"at": 0.0, "voices": []})
    for _ in range(3):
        assert asyncio.run(EL.voices()) == []
    assert len(calls) == 1
    assert calls[0]["request_options"]["timeout_in_seconds"] <= 5


# ── a refused voice says so, and is refused once, not per sentence ──────────
class _PaidPlan(Exception):
    status_code = 402
    body = {"detail": {"code": "paid_plan_required", "status": "payment_required"}}


def test_a_voice_the_plan_refuses_is_marked_and_explained(client, app, std_headers,
                                                         monkeypatch, account):
    from suni.web import server as S
    account(CLAUDIA)
    EL._refused.clear()
    _sdk(monkeypatch, _Convert(fail=_PaidPlan()))
    _speaker(monkeypatch, S, voice="eleven:claudia")
    heard = []
    _microsoft(monkeypatch, S, heard)
    app.state.eleven_tts.update(down_until=0.0, ok=False, reason=None)
    app.state.cloud_tts["down_until"] = 0.0
    try:
        r = client.post("/api/tts", json={"text": "Olá."}, headers=std_headers)
        assert r.content == b"edge-mp3", "no fallback to Microsoft"
        d = client.get("/api/tts/voices", headers=std_headers).json()
        assert d["unavailable"] == {"eleven:claudia": "paid_plan"}
        plan = client.get("/api/tts/plan", headers=std_headers).json()
        assert plan["provider"] == "microsoft" and plan["elevenlabs_reason"] == "paid_plan"
    finally:
        app.state.eleven_tts.update(down_until=0.0, ok=False, reason=None)
        EL._refused.clear()


def test_a_voice_that_works_again_is_no_longer_marked(monkeypatch, account):
    import asyncio
    account(CLAUDIA)
    EL._refused.add("claudia")
    _sdk(monkeypatch, _Convert())
    asyncio.run(EL.speak("Olá.", "eleven:claudia", "pt-PT"))
    assert not EL.refused("claudia"), "an upgraded plan still shows the voice as refused"


def test_a_replys_sentences_are_refused_once_not_each(app, std_headers, monkeypatch, account):
    """A reply's sentences reach /api/tts together. All of them were sent to
    ElevenLabs and refused in the same second, before the first refusal could
    start the rest."""
    import asyncio
    import httpx
    from suni.web import server as S
    account(CLAUDIA)
    conv = _Convert(fail=_PaidPlan())
    _sdk(monkeypatch, conv)
    _speaker(monkeypatch, S)
    _microsoft(monkeypatch, S, [])
    app.state.eleven_tts.update(down_until=0.0, ok=False, reason=None)
    app.state.cloud_tts["down_until"] = 0.0

    async def reply():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://t") as c:
            return await asyncio.gather(*(c.post("/api/tts", json={"text": f"Frase {i}."},
                                                 headers=std_headers) for i in range(3)))
    try:
        rs = asyncio.run(reply())
        assert [r.content for r in rs] == [b"edge-mp3"] * 3
        assert len(conv.calls) == 1, f"{len(conv.calls)} sentences sent to be refused"
    finally:
        app.state.eleven_tts.update(down_until=0.0, ok=False, reason=None)
        EL._refused.clear()
