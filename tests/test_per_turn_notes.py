"""A turn's notes belong to that turn.

SUNI reported it herself: the instruction to answer in European Portuguese
reached her once more on every turn — four copies on the fourth. The notes
written fresh for each turn were added to a context that lives as long as the
session, and nothing removed the previous turn's copy.
"""
import asyncio

from suni.core.base_agent import BaseAgent
from suni.core.context import Context
from suni.core.message import Message, Role
from suni.core.orchestrator import Orchestrator, _PER_TURN_NOTES
from suni.tools.registry import ToolRegistry


class _Seen(BaseAgent):
    def __init__(self):
        super().__init__(name="seen")
        self.calls = []

    async def chat(self, messages, context, tools=None) -> Message:
        self.calls.append(list(messages))
        return Message(role=Role.ASSISTANT, content="ok", agent="seen")


def _count(msgs, agent):
    return sum(1 for m in msgs if m.role == Role.SYSTEM and m.agent == agent)


def test_the_language_instruction_is_there_once_however_long_the_session():
    model = _Seen()
    orch = Orchestrator(primary=model, registry=ToolRegistry())
    ctx = Context()

    async def turns():
        for q in ("olá", "e agora?", "mais uma", "e outra"):
            await orch.run(q, ctx, response_language="pt-PT")
    asyncio.run(turns())
    assert _count(ctx.get_conversation(), "lang") == 1
    # What the model was actually handed on the last turn: the one note, plus
    # the per-call pin that keeps it last (a deliberate second position).
    assert _count(model.calls[-1], "lang") <= 2


def test_copies_already_stored_in_a_session_are_cleaned_up():
    """Sessions running when this shipped already hold the pile-up."""
    orch = Orchestrator(primary=_Seen(), registry=ToolRegistry())
    ctx = Context()
    for _ in range(4):
        ctx.add(Message(role=Role.SYSTEM, content="Responde SEMPRE em Português", agent="lang"))
        ctx.add(Message(role=Role.SYSTEM, content="[route: chat]", agent="router"))
    asyncio.run(orch.run("olá", ctx, response_language="pt-PT"))
    assert _count(ctx.get_conversation(), "lang") == 1
    assert _count(ctx.get_conversation(), "router") <= 1


def test_switching_to_english_drops_the_portuguese_instruction():
    orch = Orchestrator(primary=_Seen(), registry=ToolRegistry())
    ctx = Context()
    asyncio.run(orch.run("olá", ctx, response_language="pt-PT"))
    asyncio.run(orch.run("hello", ctx, response_language="en-GB"))
    assert _count(ctx.get_conversation(), "lang") == 0


def test_notes_from_outside_the_turn_are_kept():
    """Uploads and a sub-agent's ancestry are added by the caller before the
    turn starts; clearing them would lose the file or the delegation chain."""
    assert not ({"upload", "ancestry", "memory", "desktop"} & _PER_TURN_NOTES)
