"""
Every Ollama caller must ask for the same context size.

Two callers omitted `num_ctx` entirely — the consolidator's extraction pass and
the approval judge — and both failures were silent.

**Truncation.** Ollama falls back to a 4096 context when none is given. The
extraction prompt is EXTRACTION_BATCH conversation entries, measured at ~5,155
tokens against a real store, so roughly a fifth of the input was being cut off
before the model saw it. Nothing reported that; extraction simply had less to
work with.

**Reload thrash.** A request whose context differs from the resident model's
evicts and reloads it — verified against a live daemon: 4.7 GB at 4096, 5.1 GB
at 8192. The approval judge runs before consequential tool calls, so this landed
in the middle of ordinary use on an 8 GB card.

The value matters less than the agreement. A caller that asks for a *different*
size is as bad as one that asks for none.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Modules that talk to Ollama's chat API directly rather than through
# OllamaAgent. Each must set num_ctx on its options dict.
DIRECT_CALLERS = [
    ("suni/memory/consolidator.py", "_call_ollama"),
    ("suni/approval.py", "client.chat("),
]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel,anchor", DIRECT_CALLERS)
def test_direct_callers_set_num_ctx(rel, anchor):
    src = _src(rel)
    i = src.index(anchor)
    block = src[i:i + 1400]
    assert "num_ctx" in block, (
        f"{rel} calls Ollama without num_ctx: it will run at Ollama's 4096 "
        "default and force a model reload against every other caller")


def test_all_callers_resolve_to_the_same_value():
    """Differing values reload the model just as surely as a missing one."""
    from suni.approval import _num_ctx as approval_ctx
    from suni.memory.consolidator import _num_ctx as consolidator_ctx
    from suni.system_profile import NUM_CTX
    assert approval_ctx() == consolidator_ctx() == NUM_CTX


def test_the_agent_still_sets_it_too():
    src = _src("suni/models/ollama_agent.py")
    assert 'num_ctx' in src


@pytest.mark.parametrize("rel,anchor", DIRECT_CALLERS)
def test_the_value_is_resolved_per_call_not_at_import(rel, anchor):
    """num_ctx is editable in the admin panel. A value captured at import goes
    stale with no symptom except a reload on every request — the same trap the
    agent's own comment warns about for the host."""
    src = _src(rel)
    i = src.index("def _num_ctx")
    body = src[i:i + 500]
    assert "config" in body or "_c." in body or "_config" in body, \
        "the context is not read from config at call time"


def test_the_extraction_batch_still_fits():
    """If EXTRACTION_BATCH grows past what num_ctx holds, truncation returns —
    silently, exactly as before."""
    from suni.memory.consolidator import EXTRACTION_BATCH, _num_ctx
    # ~250 tokens per conversation entry was the measured average; leave room
    # for the system prompt and the reply.
    projected = EXTRACTION_BATCH * 250
    assert projected < _num_ctx() * 0.9, (
        f"EXTRACTION_BATCH={EXTRACTION_BATCH} projects to ~{projected} tokens "
        f"against num_ctx={_num_ctx()} — the prompt will be truncated")
