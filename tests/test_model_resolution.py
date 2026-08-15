"""
Which model SUNI runs, and where that choice comes from.

The admin panel's model selector used to have no effect across a restart:
the server read SUNI_MODEL from the environment at import, while saving the
panel only patched the live agent object. The choice applied until the next
start and was then silently discarded — the same shape as the model_chain
setting that turned out to be cosmetic.

These tests pin the precedence so that cannot regress, and assert that no
model name is baked into the shipped defaults.
"""
from __future__ import annotations

import pytest

from suni import config as suni_config
from suni import model_inventory
from suni.model_inventory import ModelInfo, resolve_model


@pytest.fixture
def isolated(monkeypatch):
    """Neutralise ambient config, environment and installed-model detection."""
    cfg: dict = {}
    monkeypatch.setattr(suni_config, "get",
                        lambda k, d=None: cfg.get(k, d if d is not None else ""))
    monkeypatch.delenv("SUNI_MODEL", raising=False)
    monkeypatch.setattr(model_inventory, "load_cached", lambda: [])
    monkeypatch.setattr(model_inventory, "tier_map", lambda inv: {})
    return cfg


def test_admin_config_wins(isolated, monkeypatch):
    """The model chosen in the admin panel takes precedence — the regression."""
    isolated["model"] = "llama3.1:8b"
    monkeypatch.setenv("SUNI_MODEL", "qwen2.5:14b")
    assert resolve_model() == "llama3.1:8b"


def test_env_used_when_nothing_configured(isolated, monkeypatch):
    """SUNI_MODEL still works for operators who prefer environment config."""
    monkeypatch.setenv("SUNI_MODEL", "mistral:7b")
    assert resolve_model() == "mistral:7b"


def test_blank_config_does_not_shadow_env(isolated, monkeypatch):
    """An empty stored value means 'not set', not 'run a model called \"\"'."""
    isolated["model"] = "   "
    monkeypatch.setenv("SUNI_MODEL", "mistral:7b")
    assert resolve_model() == "mistral:7b"


def test_falls_back_to_what_is_installed(isolated, monkeypatch):
    """With nothing configured, use the best model actually on this machine."""
    from suni.system_profile import DEFAULT_TIER
    found = ModelInfo(name="gemma2:9b", endpoint="http://localhost:11434",
                      tier=DEFAULT_TIER, params_b=9.0)
    monkeypatch.setattr(model_inventory, "tier_map", lambda inv: {DEFAULT_TIER: found})
    assert resolve_model() == "gemma2:9b"


def test_returns_empty_when_nothing_available(isolated):
    """No config, no env, no models: report unconfigured rather than inventing one."""
    assert resolve_model() == ""


def test_detection_failure_is_not_fatal(isolated, monkeypatch):
    """A broken/absent Ollama must not stop SUNI from starting."""
    def boom():
        raise OSError("ollama unreachable")
    monkeypatch.setattr(model_inventory, "load_cached", boom)
    assert resolve_model() == ""


def test_no_model_name_is_shipped_as_a_default():
    """The source ships no model name — that belongs to the deployment."""
    assert suni_config.DEFAULTS["model"] == "", \
        "config.DEFAULTS['model'] must stay empty; a shipped default overrides " \
        "the operator's choice and is wrong for anyone with different hardware"


def test_cli_and_server_resolve_identically():
    """The CLI and the web server must agree — they previously did not, and the
    CLI banner named a third value again."""
    from suni.main import _resolve_model as cli_resolver
    from suni.web.server import resolve_model as server_resolver
    assert cli_resolver is server_resolver is resolve_model
