"""
First-run wizard: who sees it, who does not, and what it is allowed to write.

Creating the admin account is not the same as being configured — a fresh
instance still has no model, language or documents chosen. The wizard fills
that gap, so the rules that matter are:

  * a genuinely new instance shows it once
  * an EXISTING install must never be dragged through it on upgrade
  * non-admins are never redirected into a wizard they cannot complete
  * skipping counts as done, or it reappears forever
"""
from __future__ import annotations

import json

import pytest

from suni import config as suni_config


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Point the config module at a throwaway file."""
    path = tmp_path / "suni_config.json"
    monkeypatch.setattr(suni_config, "_CONFIG_PATH", path)
    monkeypatch.setattr(suni_config, "_cache", {})
    return path


def test_fresh_instance_is_not_set_up(config_file):
    """No config file at all: the wizard should run."""
    assert suni_config.load()["setup_completed"] is False


def test_existing_install_is_treated_as_set_up(config_file):
    """A config file written before this feature existed belongs to a
    configured instance — upgrading must not re-run the wizard."""
    config_file.write_text(json.dumps({
        "model": "qwen2.5:7b",
        "display_name": "SUNI",
        "doc_paths": ["/srv/docs"],
    }), encoding="utf-8")
    assert suni_config.load()["setup_completed"] is True


def test_explicit_false_is_respected(config_file):
    """An instance that started the wizard and did not finish still sees it."""
    config_file.write_text(json.dumps({
        "model": "", "setup_completed": False,
    }), encoding="utf-8")
    assert suni_config.load()["setup_completed"] is False


def test_empty_config_file_is_not_mistaken_for_an_install(config_file):
    """`{}` carries no evidence of configuration, so it is still first-run."""
    config_file.write_text("{}", encoding="utf-8")
    assert suni_config.load()["setup_completed"] is False


def test_completion_is_persisted(config_file):
    """Finishing writes the flag, so the wizard does not reappear."""
    cfg = suni_config.all()
    cfg["setup_completed"] = True
    suni_config.save(cfg)
    monkey = json.loads(config_file.read_text(encoding="utf-8"))
    assert monkey["setup_completed"] is True


# ── endpoint surface ─────────────────────────────────────────────────────────

def test_wizard_writes_only_its_own_fields():
    """The endpoint must not be a second, looser config writer."""
    import inspect
    from suni.web import server
    src = inspect.getsource(server)
    body = src[src.index('@app.post("/api/setup/complete")'):]
    body = body[:body.index('@app.get("/api/auth/status")')]
    assert '"model"' in body and '"doc_paths"' in body
    for forbidden in ("smtp_pass", "telegram_bot_token", "slack_bot_token"):
        assert forbidden not in body, \
            f"the wizard must not be able to write {forbidden}"


def test_wizard_requires_admin():
    import inspect
    from suni.web import server
    src = inspect.getsource(server)
    body = src[src.index('@app.post("/api/setup/complete")'):]
    body = body[:body.index("\n    @app.get")]
    assert "require_admin" in body


def test_only_admins_are_redirected_to_the_wizard():
    """Redirecting a standard user into an admin-only wizard would lock them
    out of the app entirely."""
    import inspect
    from suni.web import server
    src = inspect.getsource(server)
    idx = src.index('RedirectResponse("/setup"')
    window = src[idx - 400:idx]
    assert 'role") == "admin"' in window or "role'] == 'admin'" in window
