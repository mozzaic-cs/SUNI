from __future__ import annotations
from .message import Message, Role
from ..system_profile import NUM_HISTORY as _NUM_HISTORY


class Context:
    def __init__(self, max_history: int = _NUM_HISTORY):
        self.history: list[Message] = []
        self.state: dict = {}
        self.max_history = max_history

    def add(self, message: Message) -> None:
        self.history.append(message)
        if len(self.history) > self.max_history:
            # Always preserve system messages; trim oldest non-system ones
            system = [m for m in self.history if m.role == Role.SYSTEM]
            rest = [m for m in self.history if m.role != Role.SYSTEM]
            keep = self.max_history - len(system)
            self.history = system + rest[-keep:]

    def get_conversation(self) -> list[Message]:
        return self.history

    def set(self, key: str, value) -> None:
        self.state[key] = value

    def get(self, key: str, default=None):
        return self.state.get(key, default)

    def clear(self) -> None:
        self.history.clear()
        self.state.clear()
