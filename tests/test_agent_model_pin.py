"""An agent profile's pinned model decides where its turn runs.

The tier loop already honoured a pin. The gap was upstream: force_claude_code
sends every turn to the CLI before that loop is reached, so a summariser pinned
to a small local model silently cost what the expensive path costs — the setting
appeared to work while deciding nothing. That switch is on in this install, so
the pin decided nothing at all.
"""
import inspect

import pytest

from suni.core import orchestrator as orch
from suni.core.orchestrator import routes_to_cli


def decide(**over):
    base = dict(pinned_model="", force_cc=False, classifier_says_yes=False,
                readonly=False, conv_mode="assistant", rbac_ok=True,
                tier_registered=True)
    base.update(over)
    return routes_to_cli(**base)


# ── the bug this fixes ───────────────────────────────────────────────────────
def test_a_local_pin_is_not_overridden_by_force_claude_code():
    assert decide(pinned_model="qwen2.5:7b", force_cc=True) is False


def test_a_local_pin_also_beats_the_classifier():
    assert decide(pinned_model="qwen2.5:7b", classifier_says_yes=True) is False


# ── the reverse pin ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["claude-code", "claude_code", "cc",
                                  "Claude-Code", "  claude-code  "])
def test_a_profile_can_pin_the_cli_with_the_switch_off(name):
    assert decide(pinned_model=name, force_cc=False) is True


# ── unchanged behaviour for everyone else ────────────────────────────────────
def test_without_a_pin_the_switch_still_decides():
    assert decide(force_cc=True) is True
    assert decide(force_cc=False) is False


def test_without_a_pin_the_classifier_still_decides():
    assert decide(classifier_says_yes=True) is True


@pytest.mark.parametrize("blocker", [
    {"readonly": True},
    {"conv_mode": "task"},
    {"rbac_ok": False},
    {"tier_registered": False},
])
def test_nothing_reaches_the_cli_through_a_closed_gate(blocker):
    # Even an explicit pin must not open read-only mode, task mode, a role that
    # is denied claude_task, or an instance with no CLI tier registered.
    assert decide(pinned_model="claude-code", force_cc=True, **blocker) is False


# ── the two halves must agree on what a pin means ────────────────────────────
def test_the_tier_loop_does_not_treat_the_cli_name_as_a_model():
    src = inspect.getsource(orch)
    i = src.index('_pin_model = (grants or {}).get("model")')
    guard = src[i:i + 500]
    assert "_CC_PIN_NAMES" in guard and '_pin_model = ""' in guard, \
        "the tier loop would try to build an Ollama client called claude-code"


def test_the_request_path_uses_this_function():
    src = inspect.getsource(orch)
    assert "_cc_should_direct = routes_to_cli(" in src, \
        "the decision was re-inlined; these tests would then prove nothing"
