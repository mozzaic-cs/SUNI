"""
Making a large local model the primary via model-chain routing.

Two things quietly undid it:

**Tier swap.** The chain agent replaced only tier 2. The inventory tiers stayed,
so complexity scoring (start tier up to MAX_LOCAL_TIER) and capability
escalation handed the turn to whatever sat at tier 3 — a weaker model, and one
that evicts the primary from the card to load.

**Context disagreement.** OllamaAgent captured NUM_CTX (derived from VRAM) while
the judge and consolidator read config `num_ctx`. Raising `num_ctx` for a model
that needs the room would have made callers disagree, which reloads the model.
"""
from __future__ import annotations

import pytest

from suni import config as suni_config
from suni.core.model_tier import CLAUDE_CODE_TIER
from suni.system_profile import DEFAULT_TIER, NUM_CTX


class _Info:
    def __init__(self, name):
        self.name, self.endpoint = name, "http://localhost:11434"


@pytest.fixture
def cfg(monkeypatch):
    store = {}
    real_get = suni_config.get
    monkeypatch.setattr(suni_config, "get",
                        lambda k, d=None: store[k] if k in store else real_get(k, d))
    return store


def _build(tier_map):
    from suni.web import server
    return server._make_backend_agents(tier_map)


def test_chain_primary_is_the_only_local_tier(cfg, monkeypatch):
    from suni.web import server
    monkeypatch.setattr(server.suni_config, "get",
                        lambda k, d=None: cfg.get(k, d), raising=False)
    cfg["model_chain_routing"] = True
    cfg["model_chain"] = [
        {"id": "tier-nano", "provider": "ollama", "model": "qwen2.5:1.5b", "enabled": False},
        {"id": "tier-core", "provider": "ollama", "model": "gpt-oss:120b", "enabled": True},
        {"id": "tier-cc", "provider": "claude-code", "model": "", "enabled": True},
    ]
    suni, tiers = _build({2: _Info("qwen2.5:7b"), 3: _Info("qwen3-coder:30b"),
                          4: _Info("llama4:latest")})
    assert suni.model == "gpt-oss:120b"
    local = {t: a for t, a in tiers.items() if t != CLAUDE_CODE_TIER}
    assert list(local) == [DEFAULT_TIER]
    assert local[DEFAULT_TIER] is suni

    # What the orchestrator actually does with a tier-3 start: walk down.
    from suni.core.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch._tier_agents, orch.primary = tiers, suni
    assert orch._agent_for_tier(3) is suni


def test_without_chain_routing_the_inventory_tiers_stay(cfg, monkeypatch):
    from suni.web import server
    monkeypatch.setattr(server.suni_config, "get",
                        lambda k, d=None: cfg.get(k, d), raising=False)
    cfg["model_chain_routing"] = False
    _, tiers = _build({2: _Info("qwen2.5:7b"), 3: _Info("qwen3-coder:30b")})
    assert {2, 3} <= set(tiers)


def test_agent_follows_configured_num_ctx(cfg):
    from suni.models.ollama_agent import OllamaAgent
    from suni.system_profile import effective_num_ctx, compress_threshold
    from suni.approval import _num_ctx as approval_ctx
    cfg["num_ctx"] = 16384
    a = OllamaAgent("t", model="gpt-oss:120b")
    assert (a.num_ctx or effective_num_ctx()) == 16384 == approval_ctx()
    assert compress_threshold() == int(16384 * 0.58)
    cfg["num_ctx"] = 0
    assert effective_num_ctx() == NUM_CTX


def test_explicit_pin_still_wins(cfg):
    from suni.models.ollama_agent import OllamaAgent
    from suni.system_profile import effective_num_ctx
    cfg["num_ctx"] = 16384
    a = OllamaAgent("t", model="x")
    a.num_ctx = 4096            # the admin panel's live-apply path
    assert (a.num_ctx or effective_num_ctx()) == 4096
