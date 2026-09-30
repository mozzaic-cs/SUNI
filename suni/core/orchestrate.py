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


# A seat whose CLI exits non-zero does not raise — the agent hands back the
# failure as its answer ("Codex returned an error (exit 1): ..."). Left alone
# that counts as a draft: it gets peer-reviewed and folded into the synthesis,
# so a broken seat does not just fail to help, it actively pollutes the answer.
_FAILED_DRAFT = (
    "returned an error (exit",
    "is not recognized as an internal",
    "command not found",
)


def _looks_like_failure(text: str) -> bool:
    """A draft that is really an error report. Matched on the shapes the agents
    actually produce, and deliberately narrow: a genuine answer that happens to
    discuss an error should still count as an answer, so this only fires on a
    SHORT response that opens with one of them."""
    t = (text or "").strip()
    if len(t) > 400:
        return False
    low = t.lower()
    return any(marker in low[:200] for marker in _FAILED_DRAFT)


async def _ask(agent, prompt: str) -> str:
    name = getattr(agent, "name", "?")
    try:
        r = await agent.chat([Message(role=Role.USER, content=prompt)], Context())
        out = (r.content or "").strip()
        if _looks_like_failure(out):
            log.warning("[COLLAB] %s answered with a failure, not a draft: %s",
                        name, out[:160].replace(chr(10), " "))
            return ""
        return out
    except Exception as e:
        log.warning("[COLLAB] %s failed: %s", name, e)
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

    # The record. Phase events go to the interface and are gone the moment the
    # turn ends; when Joaquim asked SUNI which models had answered, the honest
    # reply was that no trace of it existed. Now it does, in the same log as
    # everything else, and an operator can check her answer against it.
    log.info("[COLLAB] panel of %d: %s", len(entries),
             ", ".join(e["name"] for e in entries))

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
    silent = [e["name"] for e in entries if not e["draft"]]
    log.info("[COLLAB] drafted: %s%s",
             ", ".join(f"{e['name']} ({len(e['draft'])} chars)" for e in live) or "nobody",
             f" — no answer from {', '.join(silent)}" if silent else "")
    if not live:
        log.warning("[COLLAB] no model answered; the panel produced nothing")
        return "The collaboration models did not return an answer. Please try again."
    if len(live) == 1:
        # Loudly, because this is the failure that looks like success: the
        # answer arrives, reads perfectly well, and has had no cross-check at
        # all. Silent degradation is the whole reason the panel is worth its
        # cost being quietly cancelled.
        log.warning("[COLLAB] DEGRADED to a single model (%s): no cross-check, "
                    "no critique. Silent seats: %s",
                    live[0]["name"], ", ".join(silent) or "none")
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
        log.info("[COLLAB] critiqued: %s",
                 ", ".join(f"{e['name']}{'' if e.get('critique') else ' (silent)'}"
                           for e in live))
    else:
        log.info("[COLLAB] critique round skipped by configuration")

    # ── 3) SYNTHESIZE (the first live model merges everything) ──────────────
    _emit(event_cb, "synthesize", live[0]["name"])
    blocks = []
    for e in live:
        blocks.append(f"### Answer from model {e['name']}:\n{e['draft']}")
        if e.get("critique"):
            blocks.append(f"### {e['name']}'s critique of the others:\n{e['critique']}")
    panel = ", ".join(e["name"] for e in live)
    synth_prompt = (
        "Several expert models independently answered the task below and peer-reviewed "
        "each other. Produce the single best, most accurate and complete answer, using "
        "the critiques to resolve disagreements and correct errors. Keep what is strongest "
        "from each. Answer as yourself in a natural first-person voice — do not narrate "
        "the drafts or this process as part of an ordinary answer.\n\n"
        # The old instruction was "do NOT mention the other models" full stop,
        # and it held even when the person asked point blank which models had
        # answered: the synthesiser said it was a single model, because it had
        # been told to behave like one and has no idea it is part of a panel.
        # Concealing the process by default is a presentation choice; denying it
        # to someone who asks is a different thing, and not one to make on a
        # product that carries an AI-disclosure line.
        "If — and only if — the person is asking how this answer was produced, which "
        "models or systems were involved, or to see the reasoning behind it, then answer "
        f"that question truthfully and plainly. The facts are: this reply was produced by "
        f"a panel of {len(live)} models ({panel}), which each answered independently, then "
        "reviewed each other's answers, and you merged the results. Say so in the person's "
        "own language. Do not claim to be a single model, and do not invent details you "
        "were not given — you can see the drafts and critiques below, but not any model's "
        "internal reasoning.\n\n"
        f"TASK:\n{framed}\n\n" + "\n\n".join(blocks) + tail)
    final = await _ask(live[0]["agent"], synth_prompt)
    log.info("[COLLAB] synthesised by %s from %d draft(s): %d chars",
             live[0]["name"], len(live), len(final or live[0]["draft"]))
    return final or live[0]["draft"]
