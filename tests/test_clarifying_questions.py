"""
When SUNI should ask instead of guessing.

Joaquim asked for this: SUNI should ask when the information it has is not
enough. The obvious implementation — a bundled skill — is the wrong layer: a
skill is a recipe the model chooses to open, and a model that has recognised it
needs clarification no longer needs a recipe telling it so. This lives in the
base system prompt, which is read on every turn regardless.

The failure mode worth guarding is the opposite of the one being fixed. "Ask
when unsure" on a 7B model produces an assistant that asks which folder you
meant when there is only one folder. So the rule carries a threshold — ask only
when the answers lead to materially different work — and an explicit
counter-rule for when NOT to ask.
"""
from __future__ import annotations

import pathlib

from suni.main import SUNI_SYSTEM, resolve_system_prompt

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_rule_is_in_the_base_prompt_not_a_skill():
    """A skill would only fire once selected, which is too late."""
    assert "Ask one specific question" in SUNI_SYSTEM
    bundled = ROOT / "bundled_skills"
    slugs = {p.parent.name for p in bundled.rglob("SKILL.md")}
    assert "ask-clarifying-questions" not in slugs, \
        "clarification is default behaviour, not an opt-in recipe"


def test_it_names_a_threshold_rather_than_just_permitting_questions():
    """'Ask if unsure' with no threshold produces an assistant that asks about
    everything, which trains the user to ignore its questions."""
    assert "materially change the work" in SUNI_SYSTEM


def test_it_says_when_NOT_to_ask():
    """The counter-rule is what keeps this from becoming a nuisance."""
    assert "Do NOT ask when" in SUNI_SYSTEM
    for cue in ("sensible default", "discoverable", "assumption"):
        assert cue in SUNI_SYSTEM, f"missing the '{cue}' escape hatch"


def test_it_requires_asking_before_irreversible_actions():
    """The one case where a clear-seeming request still needs confirmation."""
    i = SUNI_SYSTEM.index("hard to undo")
    block = SUNI_SYSTEM[i:i + 220]
    assert "email" in block and "delet" in block


def test_asking_comes_before_the_work_not_after():
    assert "BEFORE doing the work" in SUNI_SYSTEM


def test_an_owner_persona_override_still_replaces_the_prompt(monkeypatch):
    """resolve_system_prompt lets an owner swap the whole persona. That must
    keep working — this change adds a line, it does not make the base prompt
    mandatory."""
    from suni import config as cfg
    monkeypatch.setattr(cfg, "get", lambda k, d=None: {
        "system_prompt": "You are a pirate.", "system_prompt_addendum": ""}.get(k, d))
    assert resolve_system_prompt() == "You are a pirate."


def test_the_addendum_is_still_appended(monkeypatch):
    from suni import config as cfg
    monkeypatch.setattr(cfg, "get", lambda k, d=None: {
        "system_prompt": "", "system_prompt_addendum": "Be brief."}.get(k, d))
    out = resolve_system_prompt()
    assert out.startswith(SUNI_SYSTEM) and out.endswith("Be brief.")


def test_the_standing_overhead_stays_bounded():
    """The base prompt AND the skill catalogue are both sent on every turn,
    before memory, document excerpts, or anything the user typed. With num_ctx
    at 8192 on the default local model that overhead is already about a quarter
    of the window — measured at ~1158 + ~825 tokens when this was written.

    The ceiling is deliberately close to the current value: the point is that
    growing either one is a decision someone has to make on purpose.
    """
    from suni.skills import SkillStore
    base = len(SUNI_SYSTEM) // 4
    catalogue = len(SkillStore().level0_context()) // 4
    assert base < 1250, f"base system prompt is ~{base} tokens on every request"
    assert base + catalogue < 2200, (
        f"standing overhead is ~{base + catalogue} tokens "
        f"({(base + catalogue) / 8192:.0%} of an 8192 context) before the user "
        "has said anything")
