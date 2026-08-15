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
"""
from __future__ import annotations
from contextvars import ContextVar


class _Acc:
    __slots__ = ("prompt", "gen", "calls")

    def __init__(self) -> None:
        self.prompt = 0
        self.gen = 0
        self.calls = 0


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
