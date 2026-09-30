"""Which processor transcribes a meeting, and why it is not always the GPU.

Asked whether transcription could use the graphics card instead of pinning
every CPU core, the obvious answer was yes: there is a second, idle card in
this machine. Measured on 120 seconds of a real meeting recording, whisper
"base":

    CPU int8              80.3s    1.5x realtime
    Quadro P600           108.2s   1.1x realtime     <- 35% SLOWER

The P600 is compute 6.1 — Pascal, no tensor cores, half precision emulated
rather than accelerated. A picker that chose the card with the most free memory
would have chosen it every single time, because the capable card in this
machine is the one the language model is sitting on.

So the test is capability, and these check the decision rather than the
hardware: the real cards are replaced with described ones, which is the only
way to cover "the big card is free" without evicting the model to find out.
"""
from __future__ import annotations

import pytest

from suni import transcription as tr

RTX4000 = {"index": 0, "name": "Quadro RTX 4000", "cap": 7.5, "free_mb": 7000}
RTX_FULL = {"index": 0, "name": "Quadro RTX 4000", "cap": 7.5, "free_mb": 216}
P600 = {"index": 1, "name": "Quadro P600", "cap": 6.1, "free_mb": 801}
P600_BIG = {"index": 1, "name": "Quadro P600", "cap": 6.1, "free_mb": 32000}


@pytest.fixture
def cards(monkeypatch):
    def _set(lst):
        monkeypatch.setattr(tr, "_cuda_cards", lambda: list(lst))
    return _set


@pytest.fixture(autouse=True)
def _auto(monkeypatch):
    """Default config, so a local suni_config.json cannot change the answer."""
    monkeypatch.setattr(tr._cfg, "get", lambda k, d=None: {
        "meeting_whisper_device": "auto",
        "meeting_whisper_device_index": -1,
    }.get(k, d))


def test_a_slow_card_is_refused_however_much_memory_it_has(cards):
    """The measurement this whole function exists for. Free memory is not the
    question; 32 GB on a Pascal would still lose to the CPU."""
    cards([P600_BIG])
    assert tr._pick_device("base") == ("cpu", 0, "int8")


def test_the_capable_card_is_used_when_it_has_room(cards):
    cards([RTX4000, P600])
    device, index, compute = tr._pick_device("base")
    assert (device, index) == ("cuda", 0), "the capable card was passed over"
    assert compute == "float16", "not using the precision the tensor cores are for"


def test_the_capable_card_is_skipped_when_the_model_has_it(cards):
    """The usual state on this machine: Ollama holds the 8 GB card, leaving a
    couple of hundred megabytes."""
    cards([RTX_FULL, P600])
    assert tr._pick_device("base") == ("cpu", 0, "int8")


def test_a_bigger_model_needs_more_room(cards):
    cards([{"index": 0, "name": "RTX 4000", "cap": 7.5, "free_mb": 1000}])
    assert tr._pick_device("tiny")[0] == "cuda", "tiny fits in a gigabyte"
    assert tr._pick_device("medium")[0] == "cpu", "medium does not"


def test_no_card_at_all_is_not_an_error(cards):
    """Most machines. It has to be the ordinary path, not the failure path."""
    cards([])
    assert tr._pick_device("base") == ("cpu", 0, "int8")


def test_the_index_can_be_forced_past_the_capability_test(monkeypatch, cards):
    """The escape hatch for hardware this was never measured on."""
    monkeypatch.setattr(tr._cfg, "get", lambda k, d=None: {
        "meeting_whisper_device": "auto",
        "meeting_whisper_device_index": 1,
    }.get(k, d))
    cards([RTX4000, P600])
    assert tr._pick_device("base")[:2] == ("cuda", 1)


def test_asking_for_the_cpu_gets_the_cpu(monkeypatch, cards):
    monkeypatch.setattr(tr._cfg, "get", lambda k, d=None: {
        "meeting_whisper_device": "cpu",
    }.get(k, d))
    cards([RTX4000])
    assert tr._pick_device("base") == ("cpu", 0, "int8")


def test_a_card_that_will_not_load_falls_back_rather_than_failing():
    """Driver and cuDNN disagreeing throws at load, not at import. Slower
    transcription beats none."""
    src = (tr.__file__).replace(".pyc", ".py")
    body = open(src, encoding="utf-8").read()
    plan = body[body.index("plan = [(device, index, compute)]"):][:400]
    assert 'plan.append(("cpu", 0, "int8"))' in plan
