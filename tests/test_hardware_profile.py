"""What this machine is, and what it is actually good at.

Three times now SUNI has chosen hardware by reasoning from a specification, and
three times the reasoning was sound and the answer was wrong:

  * the GPU with the most free memory (a Quadro P600) measured 35% SLOWER than
    the CPU for transcription;
  * so the rule became "a card with tensor cores", and that card measured 0.7x
    the CPU as well;
  * and a 120B model was made primary because it fitted in RAM, then ran at
    7.4 tok/s while `ollama ps` reported "100% GPU".

So this keeps three kinds of fact apart, and only one of them decides.
"""
from __future__ import annotations

import json
import time

import pytest

from suni import hardware as hw
from suni import system_profile as sp


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(hw, "_PATH", tmp_path / "hardware_profile.json")
    return tmp_path / "hardware_profile.json"


# ── static ───────────────────────────────────────────────────────────────────

def test_the_static_scan_answers_the_questions_that_decide_things():
    s = hw.scan_static()
    for key in ("cores", "ram_gb", "avx2", "gpus", "vram_mb_max"):
        assert key in s, f"the profile cannot answer about {key}"


def test_avx2_is_true_false_or_honestly_unknown():
    """The single fact that explains why CPU inference is slow on this box,
    and py-cpuinfo is not installed to ask the easy way. Guessing False would
    read as "this CPU is slow", which is a decision, not an observation."""
    assert hw.scan_static()["avx2"] in (True, False, None)


def test_vram_is_the_biggest_card_and_never_the_sum():
    """A model runs on ONE card. Two 4 GB cards are not an 8 GB card, and
    summing them claims a tier that neither could hold."""
    s = hw.scan_static()
    gpus = s["gpus"]
    if not gpus:
        pytest.skip("no CUDA card on this machine")
    assert s["vram_mb_max"] == max(g["vram_mb"] for g in gpus)
    if len(gpus) > 1:
        assert s["vram_mb_max"] < sum(g["vram_mb"] for g in gpus), (
            "this machine has several cards and the profile is still summing"
        )


def test_system_profile_and_the_hardware_profile_do_not_disagree():
    """Two modules answering "how much VRAM" differently is how a tier gets
    decided on a number nobody checked."""
    hw_max = hw.scan_static()["vram_mb_max"]
    if not hw_max:
        pytest.skip("no CUDA card on this machine")
    # A megabyte apart is fine: torch reports what is usable and nvidia-smi
    # reports the nominal size. A kilo apart would mean one of them is summing.
    assert abs(sp.VRAM_MB - hw_max) < 64, (
        f"system_profile says {sp.VRAM_MB}, the hardware profile says {hw_max}"
    )


def test_free_memory_is_never_written_down():
    """It changes minute to minute as the language model loads and is evicted.
    A consumer reading a remembered value would decide on a number that was
    true once."""
    s = hw.scan_static()
    assert "free_mb" not in json.dumps(s), "a volatile number is being stored"
    # ...while still being askable at the moment it is needed.
    if s["gpus"]:
        assert "free_mb" in hw.cards()[0]


# ── the file ─────────────────────────────────────────────────────────────────

def test_measurements_are_dropped_when_the_hardware_changes(profile, monkeypatch):
    """A card swap makes every number here a lie, and a lie with a timestamp
    is worse than nothing."""
    hw.ensure_scanned()
    hw.record_measurement("whisper", [{"device": "cpu", "seconds": 1.0}], {"model": "base"})
    assert hw.fastest("whisper") is not None

    real = hw.scan_static
    monkeypatch.setattr(hw, "scan_static",
                        lambda: {**real(), "gpus": [{"name": "Something Else", "vram_mb": 24576}]})
    assert hw.fastest("whisper") is None, "numbers from other hardware survived"
    assert "stale_reason" in hw.load()


def test_a_measurement_records_what_it_measured(profile):
    """"1.5x realtime" with no model, no input and no device is the kind of
    note that gets believed later because it cannot be argued with."""
    hw.ensure_scanned()
    hw.record_measurement(
        "whisper",
        [{"device": "cpu", "detail": "int8", "seconds": 80.3}],
        {"model": "base", "audio_seconds": 120, "language": "pt"},
    )
    m = hw.load()["measured"]["whisper"]
    assert m["context"]["model"] == "base"
    assert m["context"]["audio_seconds"] == 120
    assert m["at"] <= time.time()
    assert m["version"] == hw.MEASURE_VERSION


def test_the_fastest_is_the_fastest_whatever_order_it_arrived_in(profile):
    hw.ensure_scanned()
    hw.record_measurement("whisper", [
        {"device": "cuda:0", "seconds": 116.5},
        {"device": "cpu", "seconds": 80.3},
        {"device": "cuda:1", "seconds": 108.2},
    ], {"model": "base"})
    assert hw.fastest("whisper")["device"] == "cpu"


def test_a_measurement_of_a_different_shape_is_not_compared_to_this_one(profile, monkeypatch):
    hw.ensure_scanned()
    hw.record_measurement("whisper", [{"device": "cpu", "seconds": 1.0}], {})
    monkeypatch.setattr(hw, "MEASURE_VERSION", hw.MEASURE_VERSION + 1)
    assert hw.fastest("whisper") is None


def test_first_run_scans_but_never_measures(profile):
    """A benchmark on a slow machine takes minutes. It is not something to
    discover on first launch."""
    prof = hw.ensure_scanned()
    assert prof["static"]["cores"] >= 0
    assert prof["measured"] == {}, "first run ran a benchmark"


def test_benchmarks_refuse_to_start_during_a_meeting(monkeypatch):
    """It holds a card and pegs the cores for minutes — started mid-meeting it
    degrades the very transcription it is trying to speed up."""
    assert hasattr(hw, "busy_reason")
    import suni.meetings as _m
    monkeypatch.setattr(_m, "_active", {"u1": {"id": "m1"}})
    monkeypatch.setattr(_m, "active_recording", lambda u: {"id": "m1"})
    assert "meeting" in hw.busy_reason()


# ── and the thing it is all for ──────────────────────────────────────────────

def test_a_measurement_beats_a_specification(profile, monkeypatch):
    """The whole point. The capability threshold is an inference from what the
    hardware IS, and it has now been wrong twice on this machine."""
    import suni.transcription as tr
    hw.ensure_scanned()
    hw.record_measurement("whisper", [
        {"device": "cpu", "detail": "int8", "seconds": 80.3},
        {"device": "cuda:0", "detail": "float16", "seconds": 116.5},
    ], {"model": "base"})
    monkeypatch.setattr(tr._cfg, "get", lambda k, d=None: {
        "meeting_whisper_device": "auto", "meeting_whisper_device_index": -1}.get(k, d))
    # A capable card with plenty of room — the specification says use it.
    monkeypatch.setattr(tr, "_cuda_cards", lambda: [
        {"index": 0, "name": "Quadro RTX 4000", "cap": 7.5, "free_mb": 7000}])
    assert tr._pick_device("base") == ("cpu", 0, "int8"), (
        "the guess overruled the measurement"
    )
