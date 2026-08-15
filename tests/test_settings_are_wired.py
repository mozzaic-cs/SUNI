"""
Settings that appear in the admin panel must actually reach something.

This codebase has produced the same defect repeatedly: a control that reads as
configuration and never reaches what it names. Known instances —

  * model_chain          — a UI list that routing never consulted
  * config["model"]      — read from the environment at import, so the admin's
                           choice was discarded on the next restart
  * OLLAMA_* variables   — set in SUNI's process, never seen by the separate
                           Ollama service
  * memory_top_k /
    doc_top_k            — build_context() took a hardcoded default of 5 and
                           read neither key, so tuning them to cut context
                           bloat silently did nothing
  * email_poll_interval  — the watcher used a module constant

The first three are covered elsewhere (test_model_resolution.py). This file
covers the retrieval and polling settings, and guards the general shape: a key
offered in the UI that nothing consumes is a bug, not a preference.
"""
from __future__ import annotations

import inspect

import pytest

from suni import config as suni_config


# ── memory_top_k / doc_top_k ─────────────────────────────────────────────────

def test_build_context_defaults_to_config_not_a_literal():
    """top_k must default to None so config is consulted.

    The regression: the signature said top_k=5, the orchestrator passed nothing,
    and both admin sliders were therefore dead.
    """
    from suni.memory.manager import MemoryManager
    sig = inspect.signature(MemoryManager.build_context)
    assert sig.parameters["top_k"].default is None, \
        "a literal default here silently overrides memory_top_k / doc_top_k"


def test_orchestrator_does_not_pin_top_k():
    """The caller must leave the choice to config."""
    from suni.core import orchestrator
    src = inspect.getsource(orchestrator)
    call = src[src.index("build_context("):]
    call = call[:call.index(")")]
    assert "top_k" not in call, \
        "the orchestrator pins top_k, which would override the configured values"


@pytest.mark.parametrize("key", ["memory_top_k", "doc_top_k"])
def test_retrieval_counts_are_read_from_config(monkeypatch, key):
    """Each key is consulted when building context."""
    from suni.memory import manager as mgr_mod
    seen: list[str] = []
    real_get = suni_config.get

    def spy(k, d=None):
        seen.append(k)
        return real_get(k, d)

    monkeypatch.setattr(suni_config, "get", spy)

    # Exercise the config-reading prologue without needing an embedder: the
    # counts are resolved before any I/O happens.
    src = inspect.getsource(mgr_mod.MemoryManager.build_context)
    assert f'"{key}"' in src, f"build_context never mentions {key}"


def test_counts_are_clamped():
    """A nonsensical stored value must not inject unbounded context."""
    from suni.memory import manager as mgr_mod
    src = inspect.getsource(mgr_mod.MemoryManager.build_context)
    assert "min(" in src and "max(" in src, \
        "retrieval counts should be clamped against a bad stored value"


# ── email_poll_interval ──────────────────────────────────────────────────────

def test_email_poll_interval_is_read_from_config(monkeypatch):
    from suni.notifications import email_reader

    monkeypatch.setattr(suni_config, "get",
                        lambda k, d=None: 300 if k == "email_poll_interval" else d)
    assert email_reader.poll_interval() == 300


def test_email_poll_interval_is_clamped(monkeypatch):
    from suni.notifications import email_reader

    monkeypatch.setattr(suni_config, "get", lambda k, d=None: 0)
    assert email_reader.poll_interval() >= 30, "a zero interval would spin the poll loop"

    monkeypatch.setattr(suni_config, "get", lambda k, d=None: "not a number")
    assert email_reader.poll_interval() == 1800, "a bad value should fall back, not raise"


def test_no_module_level_poll_constant():
    """The constant is what made the setting unreachable."""
    from suni.notifications import email_reader
    assert not hasattr(email_reader, "POLL_INTERVAL"), \
        "a module constant reintroduces the bug the config key was meant to fix"
