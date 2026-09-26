"""Ask several specialists at once, then let the head compose one answer.

This is the delegation shape SUNI did not have. `invoke_agent` sends one task to
one agent and waits; a head of operations asks the researcher, the summariser
and the bookkeeper in parallel and writes the reply itself.

Every specialist runs through the ordinary request path, so each one keeps its
own grants (intersected with the caller's role, never widened), its own model
pin, its own daily ceiling and its own memory. Nothing here is a shortcut past
those; the fan-out only decides who is asked, how many at once, and when to stop.

Two ceilings, because parallelism multiplies cost:
  * `max_parallel` — how many run at the same time. This box has one 8 GB card,
    so two locally-pinned specialists already contend for it.
  * `token_budget` — the whole fan-out's spend. Checked BEFORE each specialist
    starts, against the tokens this turn has already booked, so an expensive
    first answer stops the rest instead of being noticed afterwards. Agents that
    do not start are reported as skipped rather than silently dropped: a summary
    built from two of five specialists must not look like a summary of five.
"""
from __future__ import annotations

import asyncio
import logging

_log = logging.getLogger("suni.core.fanout")

MAX_AGENTS = 6          # a hard ceiling on the fan; the caller's list is trimmed
DEFAULT_PARALLEL = 2


def _spent() -> int:
    """Tokens booked against this turn so far, 0 when nothing is accumulating."""
    try:
        from .. import usage as _usage
        acc = _usage.current()
        return int(getattr(acc, "prompt", 0) + getattr(acc, "gen", 0)) if acc else 0
    except Exception:      # noqa: BLE001 — a budget check never breaks a turn
        return 0


async def run_fanout(
    orchestrator,
    task: str,
    profiles: list[dict],
    *,
    user_role: str = "admin",
    user_id: str = "",
    response_language: str = "",
    event_cb=None,
    memory_for=None,
    max_parallel: int = DEFAULT_PARALLEL,
    token_budget: int = 0,
) -> list[dict]:
    """Run `task` past each profile. Returns one result dict per profile.

    Each result carries the agent's name and slug plus exactly one of
    `answer`, `error` or `skipped`, so the caller can tell a specialist that
    disagreed from one that never ran.
    """
    from .context import Context
    from .ancestry import CURRENT_REQUEST, delegation_note
    from .message import Message, Role

    picked = [p for p in (profiles or []) if p][:MAX_AGENTS]
    if not picked:
        return []

    results: list[dict] = [
        {"slug": p.get("slug", ""), "name": p.get("name") or p.get("slug", "agent")}
        for p in picked
    ]
    sem = asyncio.Semaphore(max(1, int(max_parallel or 1)))
    origin = CURRENT_REQUEST.get("")
    stop = {"hit": False}      # set once the budget is gone; later agents skip

    def _emit(phase: str, name: str, detail: str = "") -> None:
        if not event_cb:
            return
        try:
            event_cb({"type": "fanout", "phase": phase, "agent": name, "detail": detail})
            # Also as a note, which the existing clients already display.
            if phase in ("start", "skipped"):
                event_cb({"type": "cc_note",
                          "text": f"· {name}" + (f" — {detail}" if detail else "")})
        except Exception:      # noqa: BLE001
            pass

    async def _one(idx: int, profile: dict) -> None:
        slot = results[idx]
        name = slot["name"]
        async with sem:
            if stop["hit"]:
                slot["skipped"] = "the turn's token budget was spent"
                _emit("skipped", name, "budget spent")
                return
            if token_budget > 0 and _spent() >= token_budget:
                stop["hit"] = True
                slot["skipped"] = "the turn's token budget was spent"
                _log.warning("[FANOUT] budget %d spent — %r and any after it skipped",
                             token_budget, slot["slug"])
                _emit("skipped", name, "budget spent")
                return
            _emit("start", name)
            sub = Context()
            note = delegation_note(origin, name)
            if note:
                sub.add(Message(role=Role.SYSTEM, content=note, agent="ancestry"))
            try:
                slot["answer"] = await orchestrator._safe_run(
                    task, sub,
                    memory_override=memory_for(profile) if memory_for else None,
                    user_role=user_role,
                    user_id=user_id,
                    response_language=response_language,
                    agent_profile=profile,
                )
            except Exception as exc:      # noqa: BLE001 — one failure is not the turn
                slot["error"] = str(exc)
                _log.error("[FANOUT] %r failed: %s", slot["slug"], exc, exc_info=True)
            _emit("done", name)

    await asyncio.gather(*(_one(i, p) for i, p in enumerate(picked)))
    return results


def compose_prompt(task: str, results: list[dict], lang_hint: str = "") -> str:
    """The synthesis request handed to the head.

    Skipped and failed specialists are named rather than hidden: an answer built
    from two of five must not read like an answer from five.
    """
    blocks = []
    for r in results:
        if r.get("answer"):
            blocks.append(f"### {r['name']} reported:\n{r['answer']}")
        elif r.get("error"):
            blocks.append(f"### {r['name']} failed and reported nothing: {r['error']}")
        else:
            blocks.append(f"### {r['name']} did not run: "
                          f"{r.get('skipped') or 'no reason recorded'}")
    tail = ("\n\n" + lang_hint) if lang_hint else ""
    return (
        "You asked colleagues to look into the request below and they have "
        "reported back. Write the single answer yourself, in your own voice, "
        "first person. Where they disagree, say so plainly rather than averaging "
        "them. If one of them did not run or failed, and that leaves a real gap "
        "in the answer, say what is missing — do not present a partial answer as "
        "a complete one. Do not describe this process or name it as delegation.\n\n"
        f"REQUEST:\n{task}\n\n" + "\n\n".join(blocks) + tail
    )
