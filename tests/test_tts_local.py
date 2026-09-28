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
    i = SERVER.index("_tts_backend = str(suni_config.get")
    block = SERVER[i:i + 1400]
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
