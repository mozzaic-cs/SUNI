"""
OpenAI-compatible chat agent — the vLLM backend for SUNI.

vLLM exposes an OpenAI-compatible API (/v1/chat/completions, /v1/models), so this
agent talks to it via the `openai` async client. It also works against any other
OpenAI-compatible server. Ollama keeps its NATIVE agent (ollama_agent.py) because
the native path yields timing fields the dashboard uses (eval_duration/
load_duration) and keep_alive/num_ctx that /v1 can't express — so this is a
sibling, not a replacement.

Selected at runtime by config (vllm_base_url set → this agent; see models/factory).

Two response-path subtleties vs Ollama (both handled here):
  1. tool_calls[].function.arguments arrives as a JSON STRING (Ollama gives a
     dict) and each call carries a SERVER-assigned id we must preserve.
  2. vLLM strictly validates that every `tool` message is preceded by an
     `assistant` message carrying matching tool_call ids. SUNI's orchestrator
     stores tool RESULTS but not the assistant tool-call message, so _to_openai
     RECONSTRUCTS a synthetic assistant tool_calls message before each run of
     tool messages (parity with Ollama's existing context, which also omits it).

vLLM tool calling requires the server be launched with
`--enable-auto-tool-choice --tool-call-parser <model-family>`; without it, tool
calls come back as plain text in `content`. We can't fix that from here — we log
a warning when a response looks like an unparsed tool call.
"""
from __future__ import annotations
import json
import time
import uuid

from ..core.base_agent import BaseAgent
from ..core.message import Message, Role, ToolCall
from ..core.context import Context
from ..benchmarks import telemetry
from . import health as _health


class OpenAICompatAgent(BaseAgent):
    def __init__(
        self,
        name: str,
        model: str,
        system_prompt: str = "",
        host: str = "http://localhost:8000/v1",
        api_key: str = "",
    ):
        super().__init__(name)
        self.model = model
        self.system_prompt = system_prompt
        # base_url should include the /v1 suffix vLLM serves under.
        self.host = host.rstrip("/")
        # openai client refuses to init without a key; vLLM ignores it unless
        # the server was started with --api-key.
        import openai
        self._openai = openai
        self.client = openai.AsyncOpenAI(base_url=self.host, api_key=api_key or "EMPTY")
        from ..system_profile import NUM_CTX
        self.num_ctx = NUM_CTX

    async def chat(
        self,
        messages: list[Message],
        context: Context,
        tools: list[dict] | None = None,
    ) -> Message:
        oai_msgs = self._to_openai(messages)
        kwargs: dict = {"model": self.model, "messages": oai_msgs}
        if tools:
            kwargs["tools"] = tools           # already {"type":"function","function":{...}}

        # Circuit breaker: fast-fail when the backend is known-down.
        _breaker = _health.enabled()
        if _breaker and not _health.allow(self.host):
            raise _health.BackendUnavailableError(
                f"vLLM backend at {self.host} is unavailable"
            )

        _t0 = time.perf_counter()
        try:
            resp = await self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            telemetry.record(ok=False, latency_ms=(time.perf_counter() - _t0) * 1000)
            # Only connection-level failures move the breaker (request-level
            # BadRequestError etc. must not). health.is_connection_failure knows
            # the openai.* connection types.
            if _breaker and _health.is_connection_failure(exc):
                _health.record_failure(self.host)
            raise
        if _breaker:
            _health.record_success(self.host)
        _latency_ms = (time.perf_counter() - _t0) * 1000

        choice = resp.choices[0]
        msg = choice.message
        content = msg.content or ""

        # Parse tool calls: arguments is a JSON string; PRESERVE the server id.
        tool_calls: list[ToolCall] = []
        for tc in (msg.tool_calls or []):
            raw_args = tc.function.arguments or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, TypeError):
                args = {}
            tool_calls.append(ToolCall(id=tc.id or f"call_{uuid.uuid4().hex[:8]}",
                                       name=tc.function.name, args=args))

        # Tool-parser misconfiguration heuristic: model emitted a tool-call-looking
        # blob as text instead of structured tool_calls (server missing
        # --enable-auto-tool-choice / wrong --tool-call-parser).
        if not tool_calls and content and self._looks_like_unparsed_tool_call(content):
            import logging
            logging.getLogger("suni.openai_agent").warning(
                "[VLLM] response looks like an UNPARSED tool call — start vLLM with "
                "--enable-auto-tool-choice --tool-call-parser <model-family>. Host=%s",
                self.host,
            )

        # Token accounting + telemetry (no timing breakdown from OpenAI usage).
        usage = getattr(resp, "usage", None)
        prompt_tok = getattr(usage, "prompt_tokens", None) if usage else None
        gen_tok    = getattr(usage, "completion_tokens", None) if usage else None
        from .. import usage as _usage
        _usage.record(prompt_tok, gen_tok)
        telemetry.record(ok=True, latency_ms=_latency_ms,
                         prompt_tok=prompt_tok, gen_tok=gen_tok)

        note_parts = []
        if prompt_tok: note_parts.append(f"prompt {prompt_tok:,} tok")
        if gen_tok:    note_parts.append(f"gen {gen_tok} tok")
        if gen_tok and _latency_ms > 0:
            note_parts.append(f"{gen_tok / (_latency_ms / 1000):.1f} tok/s")

        out = Message(role=Role.ASSISTANT, content=content, agent=self.name,
                      tool_calls=tool_calls)
        out._trace_note = " | ".join(note_parts)
        return out

    # ── conversion ────────────────────────────────────────────────────────────

    def _to_openai(self, messages: list[Message]) -> list[dict]:
        # Merge static system prompt + injected SYSTEM messages into one block.
        system_parts = []
        if self.system_prompt:
            system_parts.append(self.system_prompt)
        for m in messages:
            if m.role == Role.SYSTEM and m.content:
                system_parts.append(m.content)

        result: list[dict] = []
        if system_parts:
            result.append({"role": "system", "content": "\n\n".join(system_parts)})

        non_sys = [m for m in messages if m.role != Role.SYSTEM]
        i = 0
        while i < len(non_sys):
            m = non_sys[i]
            if m.role == Role.TOOL:
                # Collect the consecutive run of tool results and synthesize the
                # assistant tool_calls message they must be linked to — unless the
                # previous emitted message already IS that assistant message.
                run = []
                while i < len(non_sys) and non_sys[i].role == Role.TOOL:
                    run.append(non_sys[i]); i += 1
                prev = result[-1] if result else None
                if not (prev and prev.get("role") == "assistant" and prev.get("tool_calls")):
                    result.append({
                        "role": "assistant", "content": None,
                        "tool_calls": [
                            {"id": t.tool_call_id or f"call_{k}", "type": "function",
                             "function": {"name": t.tool_name or "tool", "arguments": "{}"}}
                            for k, t in enumerate(run)
                        ],
                    })
                for k, t in enumerate(run):
                    result.append({"role": "tool",
                                   "tool_call_id": t.tool_call_id or f"call_{k}",
                                   "content": t.content})
            elif m.role == Role.ASSISTANT and m.tool_calls:
                result.append({
                    "role": "assistant", "content": m.content or None,
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.name, "arguments": json.dumps(tc.args)}}
                        for tc in m.tool_calls
                    ],
                })
                i += 1
            else:
                result.append({"role": m.role.value, "content": m.content})
                i += 1
        return result

    @staticmethod
    def _looks_like_unparsed_tool_call(content: str) -> bool:
        s = content.strip()
        return (
            ('"name"' in s and '"arguments"' in s)
            or s.startswith("<tool_call>")
            or s.startswith("<function")
        )
