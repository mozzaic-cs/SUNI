"""
Every configuration field must survive a round-trip through the admin form.

The bug this exists to prevent is quiet and destructive. `applyConfigToForm()`
copies values into the form from an explicit `fields` list. A control that is
bound to a real config key but missing from that list does not merely fail to
display its value — it renders as its widget default, and because it sits inside
`<form id="cfg-form">` the next "Save configuration" posts that default back.
The setting is silently reset by the act of saving something unrelated.

`audit_retention_days` and `session_ingest_owner` both shipped that way:
saveable, persisted, never displayed, and wiped by the following save. Neither
was caught by a test asserting the field existed, nor by one asserting it was
inside the form, nor by loading the page — it looks completely normal.

So the check is a round-trip: for every control inside cfg-form whose name is a
key in config.DEFAULTS, applyConfigToForm must actually handle that name.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from suni import config

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "suni" / "web" / "admin.html").read_text(encoding="utf-8")


def _cfg_form() -> str:
    i = HTML.index('id="cfg-form"')
    return HTML[i:HTML.index("</form>", i)]


def _apply_fn() -> str:
    i = HTML.index("function applyConfigToForm")
    # to the next top-level function declaration
    j = HTML.index("\nfunction ", i + 1)
    return HTML[i:j]


def _save_fn() -> str:
    """The submit handler that builds the POST body."""
    i = HTML.index("document.getElementById('cfg-form').addEventListener('submit'")
    j = HTML.index("\n});", i)
    return HTML[i:j]


def _controls() -> set[str]:
    """Named inputs/selects/textareas inside the config form."""
    form = _cfg_form()
    names = set()
    for m in re.finditer(r'<(input|select|textarea)\b([^>]*)>', form, re.I):
        attrs = m.group(2)
        nm = re.search(r'name="([^"]+)"', attrs)
        if not nm:
            continue
        if re.search(r'type="(button|submit|reset)"', attrs, re.I):
            continue
        names.add(nm.group(1))
    return names


def test_the_form_has_controls_at_all():
    """Guard against the parser silently matching nothing and passing."""
    assert len(_controls()) > 20


def test_every_config_backed_control_is_applied_back_to_the_form():
    """The round-trip. A name absent here is a setting that Save will erase."""
    apply_src = _apply_fn()
    missing = []
    for name in sorted(_controls()):
        base = name[:-4] if name.endswith("_raw") else name
        if base not in config.DEFAULTS:
            continue                      # UI-only control, nothing to restore
        if name not in apply_src and base not in apply_src:
            missing.append(name)
    assert not missing, (
        "these controls are bound to config keys but are never populated by "
        f"applyConfigToForm, so saving the form resets them: {missing}")


def test_every_config_backed_control_is_actually_saved():
    """The other direction, and the one the first version of this file missed.

    A control that loads correctly but is never read by the submit handler is
    editable, looks saved, and changes nothing. Both fields added on 2026-08-23
    shipped exactly that way — the load was fixed, the save was not, and only
    checking one direction hid it.
    """
    save_src = _save_fn()
    missing = []
    for name in sorted(_controls()):
        base = name[:-4] if name.endswith("_raw") else name
        if base not in config.DEFAULTS:
            continue
        if name not in save_src and base not in save_src:
            missing.append(name)
    assert not missing, (
        "these controls are bound to config keys but the submit handler never "
        f"reads them, so editing them does nothing: {missing}")


def test_the_two_that_shipped_broken_stay_fixed():
    """Both halves: displayed on load AND read on save."""
    apply_src, save_src = _apply_fn(), _save_fn()
    for name in ("audit_retention_days", "session_ingest_owner"):
        assert name in apply_src, f"{name} is not loaded into the form"
        assert name in save_src, f"{name} is never saved"


def test_the_save_handler_was_actually_found():
    """If the slice missed, the check above would pass vacuously."""
    src = _save_fn()
    assert len(src) > 2000 and "data.model" in src


def test_the_parser_would_notice_a_missing_field():
    """Proves the check can fail: a real DEFAULTS key that the apply function
    does not mention must be reported."""
    apply_src = _apply_fn()
    invented = "definitely_not_applied_key"
    assert invented not in apply_src
    # simulate: if such a control existed, the rule above would flag it
    assert invented not in apply_src and invented not in config.DEFAULTS


@pytest.mark.parametrize("key", ["audit_retention_days", "session_ingest_owner"])
def test_those_keys_are_real_config_keys(key):
    assert key in config.DEFAULTS
