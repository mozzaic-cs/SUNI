from __future__ import annotations
import anthropic
from ..core.base_agent import BaseAgent
from ..core.message import Message, Role, ToolCall
from ..core.context import Context


class ClaudeAgent(BaseAgent):
    def __init__(
        self,
        name: str = "claude",
        model: str = "claude-sonnet-4-6",
        system_prompt: str = "",
        max_tokens: int = 4096,
        api_key: str | None = None,
    ):
        super().__init__(name)
        self.model = model
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        # Explicit key when given; otherwise fall back to the ANTHROPIC_API_KEY env.
        self.client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()

    async def chat(
        self,
        messages: list[Message],
        context: Context,
        tools: list[dict] | None = None,
    ) -> Message:
        anthropic_msgs = self._to_anthropic(messages)

        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": anthropic_msgs,
        }
        if self.system_prompt:
            kwargs["system"] = self.system_prompt
        if tools:
            kwargs["tools"] = tools

        response = await self.client.messages.create(**kwargs)

        # Audit: which model actually answered (see usage.record_model).
        from .. import usage as _usage
        _usage.record_model(self.model)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, args=block.input or {})
                )

        return Message(
            role=Role.ASSISTANT,
            content="".join(text_parts),
            agent=self.name,
            tool_calls=tool_calls,
        )

    def _to_anthropic(self, messages: list[Message]) -> list[dict]:
        result: list[dict] = []
        for m in messages:
            if m.role == Role.SYSTEM:
                continue
            if m.role == Role.TOOL:
                result.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id or "",
                                "content": m.content,
                            }
                        ],
                    }
                )
            else:
                result.append({"role": m.role.value, "content": m.content})
        return result
