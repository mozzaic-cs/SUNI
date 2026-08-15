from __future__ import annotations
from abc import ABC, abstractmethod
from typing import AsyncIterator
from .message import Message
from .context import Context


class BaseAgent(ABC):
    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        context: Context,
        tools: list[dict] | None = None,
    ) -> Message:
        """Send messages and return a response. Tools are optional tool schemas."""
        ...

    async def stream(
        self,
        messages: list[Message],
        context: Context,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[str]:
        """Stream response tokens. Default: yield full response at once."""
        response = await self.chat(messages, context, tools)
        yield response.content

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
