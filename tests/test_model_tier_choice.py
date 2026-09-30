"""How big a model this machine should reach for, and why it used to overreach.

The tier was inferred from VRAM alone, and on the development box that read
8 GB as "can run a 15-44B model locally" — while a 7B already pages, because
the desktop compositor holds 2.6 GB of that card. The same reasoning is what
made a 120B the primary at 7.4 tok/s, with `ollama ps` reporting "100% GPU"
throughout.

So: measured first, and when nothing has been measured, the safe reading.
"""
from __future__ import annotations

import pytest

from suni import system_profile as sp


@pytest.fixture(autouse=True)
def _unmeasured(monkeypatch, request):
    """Default to "nobody has measured", so these test the fallback.

    Opted out with @pytest.mark.real_measure by the tests that are about
    _measured_max_tier itself — stubbing the function under test is a way to
    make a test pass while checking nothing.
    """
    if "real_measure" in request.keywords:
        return
    monkeypatch.setattr(sp, "_measured_max_tier", lambda: None)


def test_something_else_is_already_using_the_card():
    """Claiming all of it is how a 7B ends up paging while every
    specification says it fits."""
    assert sp._VRAM_RESERVE_MB >= 1024, "no allowance for anything else on the card"


def test_eight_gigabytes_is_not_a_thirty_four_billion_parameter_machine():
    """The over-claim this was written for."""
    assert sp._max_local_tier(8192) == 2, (
        "still reading an 8 GB card as able to run a 15-44B model"
    )


def test_a_tier_is_claimed_only_if_its_SMALLEST_member_fits():
    """To claim a tier you have to fit its smallest member, not its most
    flattering one. 15B at Q4 is about 9 GB before any cache."""
    from suni.core.model_tier import TIER_PARAM_RANGES
    for vram, expect in ((2_000, 1), (8_192, 2), (16_384, 3), (49_152, 4)):
        got = sp._max_local_tier(vram)
        assert got == expect, f"{vram} MB read as tier {got}, expected {expect}"
        if got == 1:
            continue        # the floor: always available, with or without a card
        lo = TIER_PARAM_RANGES[got][0]
        usable = vram - sp._VRAM_RESERVE_MB
        assert usable >= lo * 600, (
            f"tier {got} needs ~{lo * 600} MB for its smallest model, "
            f"{usable} MB usable"
        )


def test_a_tiny_card_still_gets_a_tier():
    """Never zero: there is always something it can run, or the CPU."""
    assert sp._max_local_tier(0) == 1
    assert sp._max_local_tier(512) == 1


# ── measured ────────────────────────────────────────────────────────────────

def test_a_measurement_overrules_the_reading(monkeypatch):
    monkeypatch.setattr(sp, "_measured_max_tier", lambda: 4)
    assert sp._max_local_tier(2_000) == 4, "the guess overruled the measurement"
    monkeypatch.setattr(sp, "_measured_max_tier", lambda: 1)
    assert sp._max_local_tier(49_152) == 1, "a big card overruled the stopwatch"


@pytest.mark.real_measure
def test_measured_nothing_usable_is_not_the_same_as_never_measured(monkeypatch):
    """None means nobody looked. 1 means somebody looked and nothing cleared
    the floor. Collapsing them throws away the more useful answer."""
    from suni import hardware as hw
    monkeypatch.setattr(hw, "measured", lambda kind: {
        "version": hw.MEASURE_VERSION,
        "results": [{"tier": 2, "tokens_per_sec": 4.0}],
    })
    assert sp._measured_max_tier() == 1, "a slow result should demote, not abstain"
    monkeypatch.setattr(hw, "measured", lambda kind: None)
    assert sp._measured_max_tier() is None


def test_the_floor_comes_from_a_measurement_not_a_preference():
    """gpt-oss:120b ran here at 7.4 tok/s and was unusable; a 1.5B on the same
    card managed 153. The floor sits between them, nearer the bottom."""
    assert 7.4 < sp.INTERACTIVE_FLOOR_TPS < 153


@pytest.mark.real_measure
def test_the_highest_usable_tier_wins_not_the_first(monkeypatch):
    from suni import hardware as hw
    monkeypatch.setattr(hw, "measured", lambda kind: {
        "version": hw.MEASURE_VERSION,
        "results": [
            {"tier": 1, "tokens_per_sec": 150.0},
            {"tier": 3, "tokens_per_sec": 22.0},
            {"tier": 4, "tokens_per_sec": 3.0},     # measured and too slow
        ],
    })
    assert sp._measured_max_tier() == 3
