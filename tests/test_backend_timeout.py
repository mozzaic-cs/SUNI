"""A request that never comes back, and a save that changed nothing.

Both found the same afternoon, from one symptom: a Telegram message that was
received, logged, routed — and then never answered, with nothing after the tier
line and no error anywhere.
"""
from __future__ import annotations

import pathlib

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = (ROOT / "suni/web/server.py").read_text(encoding="utf-8-sig")


def test_the_model_client_cannot_wait_for_ever():
    """ollama.AsyncClient's own default is timeout=None, which httpx reads as
    "no limit". One request that never returned took the whole turn with it:
    no reply, no error, nothing logged, and a model sitting loaded and idle.
    """
    from suni.models.ollama_agent import OllamaAgent

    t = OllamaAgent(name="t", model="qwen2.5:7b").client._client.timeout
    assert t.read is not None, "a hung generation would hang the turn for ever"
    assert t.connect is not None and t.connect <= 30, (
        "a backend that is not listening should say so at once")
    # Generous on read: generation on this hardware legitimately runs into tens
    # of seconds, and a tight cap would cut off honest work rather than hangs.
    assert t.read >= 60, f"read timeout {t.read}s would abort real generation"


def test_a_timeout_is_something_the_breaker_can_see():
    """The circuit breaker was built for exactly this and could never fire:
    it counts connection failures, and a request with no timeout never becomes
    one. It stays unreachable unless timeouts are classified as failures."""
    from suni.models import health

    for exc in (httpx.ReadTimeout("x"), httpx.ConnectTimeout("x"), TimeoutError()):
        assert health.is_connection_failure(exc), (
            f"{type(exc).__name__} does not move the breaker, so a hung "
            f"backend would never be marked down")
    # And a request-level error still must NOT open it.
    assert not health.is_connection_failure(ValueError("bad schema"))


def test_the_timeout_is_configurable_without_a_code_change():
    from suni import config

    assert config.get("ollama_timeout_s"), "no way to raise it for a slow box"
    assert config.get("ollama_connect_timeout_s")


# ── "submitted" is not "changed" ────────────────────────────────────────────
def test_config_reacts_to_what_moved_not_to_what_was_submitted():
    """The admin form posts every field on Save, so keying side effects off the
    payload's keys fires them on every save. It restarted all three channel
    gateways each time — and a restart skips the backlog, which silently ate an
    inbound Telegram message — and logged a backend "switch" to the backend
    already in use, which sent me chasing a change the user had not made.
    """
    assert "_before = {k: merged.get(k) for k in filtered}" in SERVER, (
        "nothing captures the pre-save values, so nothing can tell them apart")
    assert "_changed = {k for k, v in filtered.items() if _before.get(k) != v}" in SERVER
    # The snapshot has to be taken BEFORE the merge overwrites the old values.
    assert SERVER.index("_before = {k:") < SERVER.index("merged.update(filtered)"), (
        "the snapshot is taken after the merge, so every key looks unchanged")
    assert "_touched" not in SERVER, "something still reacts to submitted keys"


@pytest.mark.parametrize("effect", [
    "if _keys & _changed:",                       # channel supervisor
    '"model_chain_routing", "model_chain"} & _changed:',   # backend switch
])
def test_every_live_apply_is_gated_on_a_real_change(effect):
    assert effect in SERVER, f"{effect!r} still fires on an unchanged save"
