"""
Audit retention — deleting old records, and refusing to delete them by accident.

`purge_old()` existed for a long time and was never called, so the trail grew
without bound. Wiring it up is easy; wiring it up SAFELY is the part with a
sharp edge, because the config value that means "keep everything" is 0, and
`purge_old(0)` computes a cutoff of "now" and deletes the entire table. The
first test here is the one that matters.

The default is deliberately "keep everything". Two regimes disagree — AI Act
Art 26(6) wants at least six months of logs from a high-risk deployer, GDPR
Art 5(1)(e) wants personal data no longer than necessary — and SUNI cannot know
which applies, so it warns rather than choosing.
"""
from __future__ import annotations

import inspect
import pathlib

import pytest

from suni import audit

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── the guard ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("days", [0, -1, -365])
def test_purge_refuses_a_non_positive_window(days):
    """0 is the config value for 'keep everything'. Passing it through would
    delete the whole audit trail — the exact opposite of the setting."""
    with pytest.raises(ValueError):
        audit.purge_old(days)


def test_the_refusal_explains_the_zero_case():
    try:
        audit.purge_old(0)
    except ValueError as exc:
        assert "keep everything" in str(exc)


def test_retention_disabled_by_default_does_not_purge():
    """The default config must not delete anything, whatever else is true."""
    from suni import config
    assert config.DEFAULTS["audit_retention_days"] == 0
    out = audit.apply_retention(force=True)
    assert out["ran"] is False and out["deleted"] == 0
    assert "keeping everything" in out["reason"]


def test_apply_retention_never_raises(monkeypatch):
    """It is called from the scheduler loop; an exception there would stop
    scheduled runs, which is a worse outcome than a missed purge."""
    def boom(*a, **k):
        raise RuntimeError("db is gone")
    monkeypatch.setattr(audit, "purge_old", boom)
    monkeypatch.setattr(audit, "_last_retention_pass", "")
    from suni import config
    monkeypatch.setattr(config, "load", lambda: {"audit_retention_days": 30})
    out = audit.apply_retention(force=True)
    assert out["ran"] is False
    assert "purge failed" in out["reason"]


def test_a_broken_config_does_not_purge(monkeypatch):
    """Unreadable config must mean 'do nothing', never 'delete with a default'."""
    from suni import config
    def boom():
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(config, "load", boom)
    out = audit.apply_retention(force=True)
    assert out["ran"] is False and out["deleted"] == 0


def test_it_runs_at_most_once_a_day(monkeypatch):
    """The caller is a 30-second loop; without this it would purge 2880x a day."""
    calls = []
    monkeypatch.setattr(audit, "purge_old", lambda d: calls.append(d) or 0)
    monkeypatch.setattr(audit, "_last_retention_pass", "")
    from suni import config
    monkeypatch.setattr(config, "load", lambda: {"audit_retention_days": 30})
    first = audit.apply_retention()
    second = audit.apply_retention()
    assert first["ran"] is True and second["ran"] is False
    assert second["reason"] == "already ran today"
    assert len(calls) == 1


# ── it is actually wired up ──────────────────────────────────────────────────
def test_retention_is_called_by_something():
    """The defect being fixed: purge_old() existed and nothing ever called it."""
    srv = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    assert "apply_retention()" in srv, "retention is configurable but never runs"
    from tests.test_updater import _function_body
    body = _function_body(srv, "async def _schedule_runner")
    assert "apply_retention()" in body, \
        "retention is not on the loop that actually ticks"


def test_the_setting_reaches_the_config_form():
    """A field outside <form id='cfg-form'> would never be posted — this panel
    has shipped that exact bug before."""
    html = (ROOT / "suni" / "web" / "admin.html").read_text(encoding="utf-8")
    i = html.index('id="cfg-form"')
    j = html.index("</form>", i)
    assert 'name="audit_retention_days"' in html[i:j], \
        "the retention field is outside cfg-form and would never save"


def test_the_key_is_accepted_by_the_endpoint():
    """post_config filters to DEFAULTS keys, so an unknown key is dropped."""
    from suni import config
    assert "audit_retention_days" in config.DEFAULTS


# ── the warning ──────────────────────────────────────────────────────────────
def test_the_warning_covers_only_a_real_shortfall():
    """0 means keep everything — warning about it would train the operator to
    dismiss the warning that matters."""
    html = (ROOT / "suni" / "web" / "admin.html").read_text(encoding="utf-8")
    i = html.index("function retentionWarn")
    block = html[i:i + 500]
    assert "n > 0" in block and "n < 180" in block


def test_the_warning_names_both_regimes_and_decides_neither():
    js = (ROOT / "suni" / "web" / "i18n.js").read_text(encoding="utf-8")
    i = js.index('"admin.hint_retention_warn"')
    block = js[i:i + 900]
    assert "26(6)" in block and "5(1)(e)" in block, \
        "the warning cites one regime but not the other"


def test_the_warning_sits_above_the_input_not_below():
    """On a phone the on-screen keyboard covers everything under a focused
    number field — which is precisely when this warning fires, since it reacts
    to what is being typed. SUNI is used from a phone over VPN, so a warning
    rendered below the box is one the operator never sees."""
    html = (ROOT / "suni" / "web" / "admin.html").read_text(encoding="utf-8")
    warn = html.index('id="retention-warn"')
    field = html.index('id="audit_retention_days"')
    assert warn < field, "the retention warning is below the input again"


def test_the_warning_is_reapplied_after_the_config_loads():
    """Typing is not the only way the value arrives — it also arrives from the
    server, and a saved 90 must warn on arrival."""
    html = (ROOT / "suni" / "web" / "admin.html").read_text(encoding="utf-8")
    # applyConfigToForm is the function that actually writes the server's values
    # into the fields; loadConfig delegates to it.
    i = html.index("function applyConfigToForm")
    j = html.index("\n}", i)
    assert "retentionWarn()" in html[i:j], \
        "the warning only fires on input, so a stored value shows none"


def test_the_documented_gap_is_closed():
    """docs/eu-ai-act.md described retention as an honest gap. If the feature
    exists, the doc must not still claim it does not."""
    doc = ROOT / "docs" / "eu-ai-act.md"
    if not doc.exists():
        pytest.skip("no AI Act doc in this checkout")
    text = doc.read_text(encoding="utf-8")
    assert "audit_retention_days" in text, \
        "the doc still describes retention as unimplemented"
