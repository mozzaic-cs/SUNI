"""
Per-request token accounting.

Every ollama_agent.chat() already surfaces Ollama's prompt_eval_count / eval_count.
A single user REQUEST fans out into several chat() calls (tool-loop iterations,
tier escalations), so we sum them per request and attribute the total to the user.

Mechanism: a MUTABLE accumulator held in a ContextVar. The request handler binds a
fresh accumulator before running the orchestrator; each chat() call (deep in the
async call tree, same task) mutates that same object; the handler reads the total
afterward and writes it onto the request's audit row. Mutation-not-rebind is what
lets the parent see the child's updates across the await boundary.

record() is a no-op when nothing is bound, so non-chat entrypoints (that don't
start an accumulator) simply don't attribute tokens — never an error.

record_model() rides the same accumulator for a different reason: the audit row
recorded WHO asked and WHICH TOOLS ran, but never which model answered. A request
fans out across tiers and can escalate mid-run, so "the configured model" is not
the answer — only the models actually called are, in the order they were called.
Without it, "which model produced this output" cannot be reconstructed from the
log, which is the one question an incident review always asks first.

It is kept separate from record() on purpose: the delegating agents (Claude Code,
Codex) run a CLI subprocess and surface no token counts at all, so a model-name
call that had to carry token counts would simply never be made on those paths —
and those are exactly the paths where knowing what ran matters most.
"""
from __future__ import annotations
from contextvars import ContextVar

# One request that touched more than this many distinct models is a bug, not a
# workload. The cap stops a runaway loop from writing an unbounded audit cell.
_MAX_MODELS = 12


class _Acc:
    __slots__ = ("prompt", "gen", "calls", "models")

    def __init__(self) -> None:
        self.prompt = 0
        self.gen = 0
        self.calls = 0
        self.models: list[str] = []   # ordered, de-duplicated: call order is evidence


_acc: ContextVar[_Acc | None] = ContextVar("suni_usage", default=None)


def start() -> tuple[_Acc, object]:
    """Bind a fresh accumulator for this request. Returns (accumulator, token);
    pass the token to reset() in a finally."""
    acc = _Acc()
    return acc, _acc.set(acc)


def reset(token) -> None:
    try:
        _acc.reset(token)
    except Exception:
        pass


def record(prompt_tok, gen_tok) -> None:
    """Add one chat() call's token counts to the current request accumulator."""
    acc = _acc.get()
    if acc is None:
        return
    try:
        acc.prompt += int(prompt_tok or 0)
        acc.gen += int(gen_tok or 0)
        acc.calls += 1
    except (TypeError, ValueError):
        pass


def record_model(name) -> None:
    """Note that `name` served part of this request. No-op when unbound.

    Never raises: this is audit metadata riding inside the inference path, and a
    bad model name must not fail the call that produced a good answer.
    """
    acc = _acc.get()
    if acc is None:
        return
    try:
        clean = str(name or "").strip()[:80]
        if clean and clean not in acc.models and len(acc.models) < _MAX_MODELS:
            acc.models.append(clean)
    except Exception:
        pass


def models_used() -> list[str]:
    """The models that served the current request, in call order."""
    acc = _acc.get()
    return list(acc.models) if acc is not None else []
