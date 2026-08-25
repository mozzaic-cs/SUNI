"""
Skills are reached on demand, not pushed into every prompt.

The Level-0 catalogue was injected into every request until it was measured.
Eight two-tool tasks, qwen2.5:7b at temperature 0, fully-completed count:

    tools only ................ 5/8
    prompt WITHOUT catalogue .. 4/8
    prompt WITH catalogue ..... 1/8

Injecting it cost three of eight completed multi-step tasks. Two controls rule
out the obvious alternatives: an equal-sized block of neutral filler scored 6/8,
so it is not the token count; and removing the *instructions* about skills while
keeping the catalogue scored 2/8, so it is not the guidance. It is the menu —
offered a list of ready-made recipes, the model picks one and stops instead of
chaining the two tools the task needs.

These tests pin the wiring, not the measurement. The measurement lives in
scratchpad/confirm.py and can be re-run; what must not silently regress is the
catalogue creeping back into every turn, or `skills_list` disappearing and
leaving skills unreachable by any route at all.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ORCH = (ROOT / "suni" / "core" / "orchestrator.py").read_text(encoding="utf-8")


def test_the_catalogue_is_off_by_default():
    from suni import config
    assert config.DEFAULTS["skills_inject_catalogue"] is False, (
        "injecting the catalogue costs 3 of 8 completed multi-step tasks; it "
        "must be opt-in")


def test_the_injection_is_gated_by_that_key():
    i = ORCH.index("level0_context()")
    window = ORCH[max(0, i - 900):i]
    assert "skills_inject_catalogue" in window, \
        "the catalogue is built without consulting the flag"


def test_the_catalogue_has_exactly_one_route_into_context():
    """A second call site would restore the old behaviour while the flag reads
    False — the 'setting that reaches nothing' shape, inverted."""
    assert len(re.findall(r"level0_context\(\)", ORCH)) == 1


def test_skills_stay_reachable_as_a_tool():
    """On-demand only works if the model can still ask. Without skills_list the
    catalogue would be unreachable by any route, which is worse than injecting
    it."""
    from suni.main import _build_registry
    from suni import rbac
    reg = _build_registry()
    names = {(x.get("function") or x).get("name")
             for x in reg.get_ollama_tools(
                 include_prefixes=[],
                 allowed_tools=rbac.allowed_tools("admin"),
                 blocked_tools=rbac.blocked_tools("admin"))}
    assert "skills_list" in names
    assert "skill_view" in names


def test_the_prompt_still_tells_the_model_skills_exist():
    """The catalogue is gone from the prompt; the pointer to it must not be, or
    the model has no reason to call skills_list."""
    from suni.main import SUNI_SYSTEM
    assert "skills_list" in SUNI_SYSTEM


def test_turning_it_back_on_is_possible():
    """Some deployments may prefer discoverability over multi-step completion.
    The trade should be theirs, not baked in."""
    from suni import config
    assert "skills_inject_catalogue" in config.DEFAULTS


@pytest.mark.parametrize("marker", ["1/8", "4/8", "5/8"])
def test_the_measurement_is_recorded_next_to_the_code(marker):
    """A bare `if flag:` invites the next reader to flip it back. The numbers
    that justify the default belong where the decision is."""
    assert marker in ORCH, f"the {marker} result is not recorded at the call site"
