"""Speech without a round trip.

Measured on this machine, per sentence, European Portuguese:

    edge-tts (cloud)   ~2.0-2.5 s to first audio, every time
    piper (local)       0.12 s once warm, about eleven times realtime

Replies are already spoken sentence by sentence, so the cloud call is not paid
once — it is paid before the first sentence and then raced against playback for
every one after it.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = (ROOT / "suni/web/server.py").read_text(encoding="utf-8-sig")
MOD = (ROOT / "suni/tts_local.py").read_text(encoding="utf-8")

from suni import tts_local as T

_HAS_VOICE = T.available()
needs_voice = pytest.mark.skipif(not _HAS_VOICE, reason="no piper voice installed")

def _local_branch() -> str:
    """The local-voice branch, to where the cloud branch begins.

    Sliced to its real end rather than a fixed character count: a comment added
    at the top pushed the assertion out of a 1400-character window and failed
    the test over prose. That is the third time today a slice-sized test has
    broken on an insertion.
    """
    i = SERVER.index("_tts_backend = str(suni_config.get")
    j = SERVER.index("if _saved_voice in _EDGE_TTS_VOICE_SET:", i)
    return SERVER[i:j]



def test_synthesis_never_runs_on_the_event_loop():
    """Piper is CPU-bound ONNX. This codebase has starved its event loop three
    times — a per-file index write, an os.walk, a 2 MB state save — and each
    one presented as "SUNI is hanging" rather than as a blocked thread."""
    assert "asyncio.to_thread(_synth_sync" in MOD, "synthesis blocks the loop"
    assert "asyncio.to_thread(_load_sync" in MOD, "loading blocks the loop"


def test_a_missing_or_broken_voice_costs_nobody_their_speech():
    """A voice that will not load on some other machine, an unsupported
    language, a corrupt file: none of those should silence her."""
    assert "return None" in MOD
    block = _local_branch()
    # It returns only when there IS audio, and swallows its own failures — so
    # control reaches the cloud path below in every other case.
    assert "if _wav:" in block, "an empty local result is returned as audio"
    assert "except Exception" in block, "a local failure propagates instead of falling back"
    assert block.count("return Response") == 1, "the local branch returns on failure too"


def test_the_voice_is_found_wherever_the_process_was_started():
    """A relative model path is a promise about the working directory, which a
    service or a test harness breaks without telling anybody — and the failure
    is silent: it just uses the cloud voice for ever."""
    assert 'Path(__file__).resolve()' in MOD, "the model directory is CWD-relative"
    assert 'Path("models/piper")' not in MOD


def test_the_voice_is_loaded_once_not_per_request():
    """Loading costs ~5 s and the first synthesis after it another ~2 s of ONNX
    warm-up — the entire saving, thrown away once per request."""
    assert "_voice = v" in MOD and "if _voice is not None" in MOD
    assert "async def warm(" in MOD
    assert "_tts_local.warm()" in SERVER, "nothing warms it at startup"


def test_startup_does_not_wait_for_a_voice():
    i = SERVER.index("_tts_local.warm()")
    assert "create_task" in SERVER[i - 120:i + 40], "startup blocks on the voice"


def test_the_backend_choice_is_configurable_and_defaults_to_auto():
    from suni import config
    assert "tts_backend" in config.DEFAULTS
    assert config.DEFAULTS["tts_backend"] == "auto", (
        "a new install should use the local voice when one is present")


def test_the_model_is_not_committed():
    """63 MB of ONNX per voice, downloaded from the piper project: a build
    artefact, not source — and this repo is public."""
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "models/piper" in ignore


@needs_voice
def test_it_actually_speaks():
    data = asyncio.run(T.speak("Tudo em ordem, obrigado."))
    assert data and len(data) > 4000, "no audio came back"
    assert data[:4] == b"RIFF", "not a WAV the browser can play"


@needs_voice
def test_empty_text_asks_for_nothing():
    assert asyncio.run(T.speak("   ")) is None


@needs_voice
def test_it_is_fast_enough_to_be_worth_it():
    """The whole point is beating a ~2 s cloud round trip. If it cannot, the
    added dependency is not carrying its weight and should be questioned."""
    import time

    asyncio.run(T.speak("aquecimento"))          # pay the ONNX warm-up first
    t0 = time.perf_counter()
    asyncio.run(T.speak("Os circuitos estão estáveis e o humor está seco."))
    took = time.perf_counter() - t0
    assert took < 1.5, f"{took:.2f}s — no better than the cloud call it replaces"


@needs_voice
def test_the_loop_survives_synthesis():
    """The test above proves it is fast; this proves it is not fast at the cost
    of everything else on the loop."""
    async def main():
        ticks = 0

        async def tick():
            nonlocal ticks
            for _ in range(30):
                await asyncio.sleep(0.01)
                ticks += 1

        await asyncio.gather(
            T.speak("Uma frase suficientemente longa para medir o bloqueio."),
            tick())
        return ticks

    assert asyncio.run(main()) == 30, "the event loop stalled during synthesis"


# ── a voice somebody chose must win ─────────────────────────────────────────
def test_an_explicitly_chosen_voice_is_not_overridden():
    """The first version ran the local branch before reading the saved voice,
    so a female voice somebody had selected silently became the male one that
    happens to be the only European Portuguese voice piper ships. Their setting
    reached nothing. Speed is not worth taking a choice away."""
    block = _local_branch()
    assert "_chose_edge" in block, "the saved voice is not consulted"
    assert "_saved_voice in _EDGE_TTS_VOICE_SET" in block
    assert 'not _chose_edge' in block, "auto still overrides a chosen voice"
    # And the saved voice has to be read BEFORE the decision, not after.
    assert SERVER.index("_saved_voice = str(") < SERVER.index(
        "_tts_backend = str(suni_config.get")


def test_the_local_voice_can_be_chosen_on_purpose():
    """A voice that cannot be selected is not an option, it is an ambush: the
    only way it could ever play was by overriding something."""
    i = SERVER.index("async def tts_voices")
    block = SERVER[i:i + 900]
    assert '"local:"' in block or "f\"local:{p.stem}\"" in block, (
        "the picker lists only cloud voices, so local is unchoosable")
    assert "_chose_local" in _local_branch(), "choosing it has no effect"


@pytest.mark.parametrize("saved,backend,expect_local", [
    ("pt-PT-RaquelNeural", "auto",  False),   # chosen cloud voice — honoured
    ("",                   "auto",  True),    # nothing chosen — local is the faster default
    ("local:pt_PT-tugao",  "auto",  True),    # chosen local voice
    ("pt-PT-RaquelNeural", "piper", True),    # pinned by the operator, deliberately
])
def test_the_decision_table_holds(saved, backend, expect_local):
    edge_voices = {"en-GB-SoniaNeural", "pt-PT-RaquelNeural"}
    chose_edge = saved in edge_voices
    chose_local = saved.startswith("local:")
    use_local = chose_local or backend in ("piper", "local") or (
        backend == "auto" and not chose_edge)
    assert use_local is expect_local


# ── whose language is it ────────────────────────────────────────────────────
def test_transcription_uses_the_speakers_language_not_the_instance_default():
    """Measured, not argued. The same clip of European Portuguese:

        told "pt-PT"  -> "Quer ver os ficheres indexados com o resumo por cliente."
        told "en-GB"  -> "want to see the indexed screws with the client's resume."
        told nothing  -> "want to see the indexed screws with the client's resume."

    The instance default is en-GB and this speaker is pt-PT, so reading config
    instead of the user's settings had whisper transcribing Portuguese speech
    into English — and it was confident about it, which is how it reached the
    user as a plausible-looking sentence rather than as an error.
    """
    stt_src = (ROOT / "suni/stt.py").read_text(encoding="utf-8")
    assert "language: str = \"\"" in stt_src, "transcribe() cannot be told a language"
    i = stt_src.index("async def _transcribe_local")
    block = stt_src[i:i + 1800]
    assert "language or" in block, "the caller's language is ignored"

    server = (ROOT / "suni/web/server.py").read_text(encoding="utf-8-sig")
    j = server.index("async def stt_transcribe")
    route = server[j:j + 1800]
    assert "_user_settings.get(user[\"id\"])" in route, (
        "the route sends the instance default rather than the speaker's language")
    assert "language=_stt_lang" in route


def test_detection_is_not_trusted_over_a_stated_language():
    """I claimed detection beats a possibly-wrong label, then measured it: on a
    short clip whisper detected English for Portuguese speech. A stated
    language wins, and the instance default is the last resort rather than
    letting it guess."""
    stt_src = (ROOT / "suni/stt.py").read_text(encoding="utf-8")
    i = stt_src.index("async def _transcribe_local")
    block = stt_src[i:i + 1800]
    assert '_cfg.get("stt_language"' in block, "nothing to fall back to"
    assert block.index("language or") < block.index('_cfg.get("stt_language"'), (
        "the instance default is consulted before the speaker's own language")


# ── when the cloud voice refuses ────────────────────────────────────────────
class _Refused:
    """edge_tts.Communicate as it behaves here today: Microsoft answers 403."""
    calls = 0

    def __init__(self, text, voice):
        type(self).calls += 1

    async def stream(self):
        raise RuntimeError("403, message='Invalid response status'")
        yield  # pragma: no cover


def _cloud_only(monkeypatch, app, spoken):
    from suni import config as C
    from suni.web import server as S
    real_get = C.get
    # A backend that is neither auto nor local: the cloud is tried first.
    monkeypatch.setattr(C, "get", lambda k, d=None: "edge" if k == "tts_backend"
                        else real_get(k, d))
    _Refused.calls = 0
    monkeypatch.setattr(S.edge_tts, "Communicate", _Refused)
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)   # edge-tts route

    async def speak(text):
        spoken.append(text)
        return b"RIFFfakewav"
    monkeypatch.setattr(T, "available", lambda: True)
    monkeypatch.setattr(T, "speak", speak)
    app.state.cloud_tts["down_until"] = 0.0


def test_a_refused_cloud_voice_falls_back_to_the_local_one(client, app, std_headers,
                                                            monkeypatch):
    """Her chosen voice is a cloud one; when Microsoft refuses it she went
    silent — no speech, no mouth, no progress updates. A different voice is
    better than none."""
    spoken = []
    _cloud_only(monkeypatch, app, spoken)
    try:
        r = client.post("/api/tts", json={"text": "Olá."}, headers=std_headers)
        assert r.status_code == 200, r.status_code
        assert r.headers["content-type"] == "audio/wav"
        assert spoken == ["Olá."]
        assert _Refused.calls == 1
    finally:
        app.state.cloud_tts["down_until"] = 0.0


def test_a_refused_cloud_voice_is_not_retried_before_every_sentence(client, app,
                                                                    std_headers,
                                                                    monkeypatch):
    spoken = []
    _cloud_only(monkeypatch, app, spoken)
    try:
        for t in ("Um.", "Dois.", "Três."):
            assert client.post("/api/tts", json={"text": t},
                               headers=std_headers).status_code == 200
        assert _Refused.calls == 1, "the refused cloud is paid for on every sentence"
        assert spoken == ["Um.", "Dois.", "Três."]
        # ...and is given another chance once the pause is over.
        app.state.cloud_tts["down_until"] = 0.0
        client.post("/api/tts", json={"text": "Quatro."}, headers=std_headers)
        assert _Refused.calls == 2
    finally:
        app.state.cloud_tts["down_until"] = 0.0


def test_self_hosted_tts_still_never_falls_back():
    """A privacy deployment returns before the fallback: its 503 stays a 503."""
    i = SERVER.index("if _tts.enabled():")
    j = SERVER.index("_tts_backend = str(suni_config.get", i)
    assert "return Response(status_code=503)" in SERVER[i:j]
    assert SERVER.index("local fallback failed too") > j


# ── Windows' own voices (Helia, pt-PT, female, local) ───────────────────────
def _fake_windows(monkeypatch, said):
    from suni import tts_windows as Wn
    monkeypatch.setattr(Wn, "voices", lambda: [("Microsoft Helia - Portuguese (Portugal)", "pt-PT"),
                                               ("Microsoft Zira - English (United States)", "en-US")])

    async def speak(text, vid):
        said.append(vid)
        return b"RIFFwinwav"
    monkeypatch.setattr(Wn, "speak", speak)


def test_a_refused_cloud_voice_falls_back_to_a_windows_voice_in_its_language(
        client, app, std_headers, monkeypatch):
    """Raquel is pt-PT and female; the piper voice is male. Helia is the
    nearer substitute, so she goes first."""
    from suni.web import server as S
    spoken, said = [], []
    _cloud_only(monkeypatch, app, spoken)
    _fake_windows(monkeypatch, said)
    monkeypatch.setattr(S, "_voice_for_lang", lambda lang: "pt-PT-RaquelNeural")
    try:
        r = client.post("/api/tts", json={"text": "Olá."}, headers=std_headers)
        assert r.status_code == 200 and r.content == b"RIFFwinwav"
        assert said == ["win:Microsoft Helia - Portuguese (Portugal)"]
        assert spoken == [], "the male piper voice was used though Helia was there"
    finally:
        app.state.cloud_tts["down_until"] = 0.0


def test_a_windows_voice_can_be_chosen_and_is_listed(client, std_headers, monkeypatch):
    said = []
    _fake_windows(monkeypatch, said)
    voices = client.get("/api/tts/voices", headers=std_headers).json()["voices"]
    assert "win:Microsoft Helia - Portuguese (Portugal)" in voices
    # chosen on purpose → used, before any cloud or piper attempt
    assert "_win.speak(clean, _saved_voice)" in SERVER
    assert SERVER.index("_win.speak(clean, _saved_voice)") < SERVER.index("_local.speak(clean)")


def test_windows_voices_are_rendered_off_the_event_loop():
    mod = (ROOT / "suni/tts_windows.py").read_text(encoding="utf-8")
    assert "asyncio.to_thread(_synth_sync" in mod
    assert "CoInitialize" in mod, "COM used from a worker thread without initialising it"
    assert "SVSFIsNotXML" in mod, "reply text would be parsed as SAPI markup"


# ── Azure Speech: the official route to the same neural voices ──────────────
def _azure(monkeypatch, answer):
    """httpx as Azure would answer: (status, body). Records what was sent."""
    import httpx
    sent = []

    def handler(req):
        sent.append(req)
        return httpx.Response(answer[0], content=answer[1])
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("AZURE_SPEECH_KEY", "k-test")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "WestEurope")
    return sent


def test_azure_speaks_the_chosen_voice_instead_of_edge_tts(client, app, std_headers,
                                                          monkeypatch):
    from suni.web import server as S
    spoken = []
    _cloud_only(monkeypatch, app, spoken)
    sent = _azure(monkeypatch, (200, b"ID3mp3"))
    monkeypatch.setattr(S, "_voice_for_lang", lambda lang: "pt-PT-RaquelNeural")
    try:
        r = client.post("/api/tts", json={"text": "Olá <b> & adeus."}, headers=std_headers)
        assert r.status_code == 200 and r.content == b"ID3mp3"
        assert r.headers["content-type"] == "audio/mpeg"
        assert _Refused.calls == 0, "edge-tts still tried with Azure configured"
        req = sent[0]
        assert str(req.url) == ("https://westeurope.tts.speech.microsoft.com"
                                "/cognitiveservices/v1")
        assert req.headers["Ocp-Apim-Subscription-Key"] == "k-test"
        body = req.content.decode()
        assert "name='pt-PT-RaquelNeural'" in body.replace('"', "'")
        assert "&lt;b&gt; &amp; adeus" in body, "reply text parsed as SSML markup"
    finally:
        app.state.cloud_tts["down_until"] = 0.0


def test_azure_refusing_falls_back_and_never_logs_the_key(client, app, std_headers,
                                                          monkeypatch, caplog):
    """Free tier used up (429) or a wrong key (401): she keeps talking."""
    spoken = []
    _cloud_only(monkeypatch, app, spoken)
    _azure(monkeypatch, (429, b""))
    try:
        r = client.post("/api/tts", json={"text": "Olá."}, headers=std_headers)
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
        assert app.state.cloud_tts["down_until"] > 0, "refused Azure retried every sentence"
        assert "k-test" not in caplog.text
    finally:
        app.state.cloud_tts["down_until"] = 0.0


def test_azure_is_off_until_both_values_are_set(monkeypatch):
    from suni import tts_azure as A
    monkeypatch.setenv("AZURE_SPEECH_KEY", "k")
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    assert not A.configured()
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
    assert A.configured()
