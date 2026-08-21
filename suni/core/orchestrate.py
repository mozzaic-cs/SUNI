"""
Multi-model collaboration engine — SUNI's "orchestrate" mode (Mode 2).

An explicit, opt-in mode where two or more capable/frontier models COLLABORATE to
reach the best, most accurate, most complete answer — as opposed to Mode 1's fast
single-model tier pipeline. The value comes from DECORRELATED models (e.g. Claude
Code + Codex/ChatGPT): each catches what the other misses.

Pattern (conductor-worker, not peer debate):
  1. DRAFT      — every model answers the task independently (parallel)
  2. CRITIQUE   — each model peer-reviews the others' drafts (parallel)
  3. SYNTHESIZE — one model merges drafts + critiques into a single best answer,
                  spoken as SUNI (the collaboration stays backstage)

Provider-agnostic and works with 1..N models, but a lone model self-critiquing is
a weak critic (correlated errors) — the real payoff needs ≥2 different providers.

PRIVACY: these are cloud frontier models; Mode-2 data leaves the box. Mode 1 stays
the local/private path. The mode is a knowing, per-message choice.
"""
from __future__ import annotations
import asyncio
import logging
from .message import Message, Role
from .context import Context

log = logging.getLogger("suni.orchestrate")


def _emit(event_cb, phase: str, detail: str = "") -> None:
    if event_cb:
        try:
            event_cb({"type": "collab", "phase": phase, "detail": detail})
        except Exception:
            pass


async def _ask(agent, prompt: str) -> str:
    try:
        r = await agent.chat([Message(role=Role.USER, content=prompt)], Context())
        return (r.content or "").strip()
    except Exception as e:
        log.warning("[COLLAB] %s failed: %s", getattr(agent, "name", "?"), e)
        return ""


def _build_pool(pool: list | None = None):
    """Construct the collaboration agents from `pool`, else config
    `collaborate_pool` (default: Claude Code + Codex — the two no-key frontier
    providers)."""
    from .. import config as _cfg
    from ..models import factory as _factory
    pool = pool or _cfg.get("collaborate_pool") or [
        {"provider": "claude-code", "model": ""},
        {"provider": "codex",       "model": ""},
    ]
    agents = []
    for i, e in enumerate(pool):
        if not isinstance(e, dict) or not e.get("enabled", True):
            continue
        try:
            a = _factory.make_provider_agent(
                f"collab_{i}", e.get("provider", ""), e.get("model", ""),
                e.get("base_url", ""), e.get("api_key", ""), "")
            agents.append(a)
        except Exception as ex:
            log.warning("[COLLAB] pool build failed for %s: %s", e.get("provider"), ex)
    return agents


async def run_collaboration(task: str, event_cb=None, lang_hint: str = "",
                            context_hint: str = "", pool: list | None = None,
                            persona: str = "") -> str:
    """Entry point: build the pool from config and run draft→(critique)→synthesize.

    pool:    per-agent override of the global collaborate_pool, so one profile can
             convene a different panel from another — a reviewer wanting two
             frontier models is not the same panel as a summariser.
    persona: the agent's own instructions, carried into the synthesis so the
             answer sounds like that agent rather than like generic SUNI. The
             draft and critique stages stay neutral on purpose: a persona applied
             to every seat would correlate the models, and decorrelation is the
             entire reason the panel is worth its cost.
    """
    from .. import config as _cfg
    agents = _build_pool(pool)
    if persona:
        _sep = chr(10) * 2
        _head = "[Answer as this agent, in its voice and remit]" + chr(10)
        context_hint = ((context_hint + _sep) if context_hint else "") + _head + persona.strip()
    skip_critique = bool(_cfg.get("collaborate_skip_critique", False))
    return await collaborate(task, agents, event_cb=event_cb, lang_hint=lang_hint,
                             context_hint=context_hint, skip_critique=skip_critique)


async def collaborate(task: str, agents: list, event_cb=None,
                      lang_hint: str = "", context_hint: str = "",
                      skip_critique: bool = False) -> str:
    entries = [{"name": getattr(a, "name", "model"), "agent": a} for a in agents if a]
    if not entries:
        return ("Collaboration mode has no models available. Configure at least one "
                "capable provider (e.g. Claude Code or Codex) to use this mode.")

    framed = task
    if context_hint:
        framed = f"[Conversation so far]\n{context_hint}\n\n[Current request]\n{task}"
    tail = ("\n\n" + lang_hint) if lang_hint else ""

    # ── 1) DRAFT (parallel) ────────────────────────────────────────────────
    _emit(event_cb, "draft", ", ".join(e["name"] for e in entries))
    drafts = await asyncio.gather(*[_ask(e["agent"], framed + tail) for e in entries])
    for e, d in zip(entries, drafts):
        e["draft"] = d
    live = [e for e in entries if e["draft"]]
    if not live:
        return "The collaboration models did not return an answer. Please try again."
    if len(live) == 1:
        _emit(event_cb, "single", live[0]["name"])
        return live[0]["draft"]   # only one model responded — degraded, no cross-check

    # ── 2) CROSS-CRITIQUE (parallel) ───────────────────────────────────────
    # Fast dial: skip this round (draft→synthesize only) — ~40% fewer calls, but
    # the synthesizer loses the decorrelated peer-review that catches blind spots.
    if not skip_critique:
        _emit(event_cb, "critique", ", ".join(e["name"] for e in live))

        async def _critique(e):
            others = "\n\n".join(f"--- Answer from model {o['name']} ---\n{o['draft']}"
                                 for o in live if o is not e)
            prompt = (
                "You are peer-reviewing other expert models' answers to a task. "
                "Point out any factual errors, missing considerations, or weak reasoning, "
                "and note what each does best. Be specific and brief — a critique, not a rewrite.\n\n"
                f"TASK:\n{task}\n\n{others}")
            return await _ask(e["agent"], prompt)

        crits = await asyncio.gather(*[_critique(e) for e in live])
        for e, c in zip(live, crits):
            e["critique"] = c

    # ── 3) SYNTHESIZE (the first live model merges everything) ──────────────
    _emit(event_cb, "synthesize", live[0]["name"])
    blocks = []
    for e in live:
        blocks.append(f"### Answer from model {e['name']}:\n{e['draft']}")
        if e.get("critique"):
            blocks.append(f"### {e['name']}'s critique of the others:\n{e['critique']}")
    synth_prompt = (
        "Several expert models independently answered the task below and peer-reviewed "
        "each other. Produce the single best, most accurate and complete answer, using "
        "the critiques to resolve disagreements and correct errors. Keep what is strongest "
        "from each. Answer as yourself in a natural first-person voice — do NOT mention the "
        "other models, the drafts, or this synthesis process.\n\n"
        f"TASK:\n{framed}\n\n" + "\n\n".join(blocks) + tail)
    final = await _ask(live[0]["agent"], synth_prompt)
    return final or live[0]["draft"]
