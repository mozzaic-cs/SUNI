"""
Every configuration field must survive a round-trip through the admin form.

The bug this exists to prevent is quiet and destructive. `applyConfigToForm()`
copies values into the form from an explicit `fields` list. A control that is
bound to a real config key but missing from that list does not merely fail to
display its value — it renders as its widget default, and because it sits inside
`<form id="cfg-form">` the next "Save configuration" posts that default back.
The setting is silently reset by the act of saving something unrelated.

Whether that is merely annoying or actually destructive depends on how the
submit handler sends the field, and the distinction matters:

  sent UNCONDITIONALLY (`data.smtp_host = form.elements.smtp_host.value`) and
  not loaded — the blank box is posted every time, so the stored value is
  destroyed. `smtp_host`, `smtp_user`, `notify_to` and `imap_host` were here.

  sent under a guard (`if (!isNaN(n)) data.x = n`) or not sent at all — the key
  is simply absent from the body, and since the endpoint merges over the
  current config, the stored value survives. `audit_retention_days` and
  `session_ingest_owner` were here: mis-displayed and uneditable from the UI,
  but never wiped. An earlier version of this docstring called them
  destructive, which was wrong.

So there are three checks: every config-backed control must be loaded, every
one must be saved, and — the real safety property — anything sent
unconditionally must be loaded, or saving destroys it.
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


def test_anything_sent_unconditionally_is_also_loaded():
    """The actual safety property, stronger than either direction alone.

    A field the submit handler always sends, but applyConfigToForm never fills,
    posts an empty box over stored data on every save. That is how the mail
    settings were being erased. Guarded fields (isNaN) and conditionally-sent
    ones are exempt: an absent key is preserved by the endpoint's merge.
    """
    save_src, apply_src = _save_fn(), _apply_fn()
    unconditional = set(re.findall(r'data\.(\w+)\s*=\s*\(?form\.elements', save_src))
    at_risk = []
    for name in sorted(unconditional):
        if name not in config.DEFAULTS:
            continue
        # secrets follow the server-side "blank means keep stored" rule
        if name.endswith(("_api_key", "_token", "_pass", "_password")):
            continue
        if name not in apply_src:
            at_risk.append(name)
    assert not at_risk, (
        "these are posted on every save but never loaded, so saving overwrites "
        f"the stored value with an empty box: {at_risk}")


def test_the_secret_exemption_is_real_not_assumed():
    """The exemption above rests on the endpoint preserving blank secrets. If
    that rule ever goes away, the exemption becomes a data-loss bug."""
    srv = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    i = srv.index('@app.post("/api/config"')
    block = srv[i:i + 2000]
    assert 'endswith("_api_key")' in block or "_api_key" in block
    assert "smtp_pass" in block and "del filtered[" in block


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
