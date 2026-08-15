from __future__ import annotations
import time
import uuid
import ollama
from rich.console import Console
from ..core.base_agent import BaseAgent
from ..core.message import Message, Role, ToolCall
from ..core.context import Context
from ..benchmarks import telemetry
from . import health as _health

_console = Console(stderr=True)


class OllamaAgent(BaseAgent):
    def __init__(
        self,
        name: str,
        model: str,
        system_prompt: str = "",
        host: str = "",
    ):
        super().__init__(name)
        self.model = model
        self.system_prompt = system_prompt
        from .. import config as _cfg
        # Resolved here rather than in the signature: a default argument is
        # evaluated at import, which no configuration can move afterwards.
        self.host = host or _cfg.ollama_host()
        self.client = ollama.AsyncClient(host=self.host)
        from ..system_profile import NUM_CTX
        self.num_ctx = NUM_CTX

    async def chat(
        self,
        messages: list[Message],
        context: Context,
        tools: list[dict] | None = None,
    ) -> Message:
        ollama_msgs = self._to_ollama(messages)
        kwargs: dict = {"model": self.model, "messages": ollama_msgs}
        if tools:
            kwargs["tools"] = tools

        kwargs["options"] = {"num_ctx": self.num_ctx, "num_batch": 1024}
        kwargs["keep_alive"] = -1  # keep model resident between requests

        # Circuit breaker: if the backend is known-down, fail fast instead of
        # hanging on a doomed connection and thrashing through tier escalation.
        _breaker = _health.enabled()
        if _breaker and not _health.allow(self.host):
            raise _health.BackendUnavailableError(
                f"Local model backend at {self.host} is unavailable"
            )

        _t0 = time.perf_counter()
        try:
            response = await self.client.chat(**kwargs)
        except Exception as exc:
            # Record the failure for the live error-rate metric, then re-raise
            # so existing error handling upstream is unchanged. Only CONNECTION
            # failures move the breaker — a request-level error (context overflow,
            # bad schema, ollama.ResponseError) must not open it.
            telemetry.record(ok=False, latency_ms=(time.perf_counter() - _t0) * 1000)
            if _breaker and _health.is_connection_failure(exc):
                _health.record_failure(self.host)
            raise
        if _breaker:
            _health.record_success(self.host)
        _latency_ms = (time.perf_counter() - _t0) * 1000
        msg = response.message

        # Build trace note from Ollama's built-in timing stats
        prompt_tok    = getattr(response, "prompt_eval_count", None)
        gen_tok       = getattr(response, "eval_count", None)
        eval_ns       = getattr(response, "eval_duration", None)
        load_ns       = getattr(response, "load_duration", None)
        prompt_eval_ns = getattr(response, "prompt_eval_duration", None)

        # Per-request token accounting: sum this call into the request's
        # accumulator (no-op if none is bound). Durable per-user totals are
        # written to the audit row by the request handler.
        from .. import usage as _usage
        _usage.record(prompt_tok, gen_tok)

        # Feed the passive telemetry ring buffer (live dashboard metrics).
        telemetry.record(
            ok=True, latency_ms=_latency_ms,
            load_ns=load_ns, prompt_eval_ns=prompt_eval_ns, eval_ns=eval_ns,
            prompt_tok=prompt_tok, gen_tok=gen_tok,
        )
        note_parts = []
        if prompt_tok: note_parts.append(f"prompt {prompt_tok:,} tok")
        if gen_tok:    note_parts.append(f"gen {gen_tok} tok")
        if gen_tok and eval_ns and eval_ns > 0:
            tps = gen_tok / (eval_ns / 1e9)
            note_parts.append(f"{tps:.1f} tok/s")
        if load_ns and load_ns > 500_000_000:  # only note if > 0.5s (model was cold)
            note_parts.append(f"load {load_ns/1e9:.1f}s")
        trace_note = " | ".join(note_parts)

        tool_calls: list[ToolCall] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=str(uuid.uuid4()),
                        name=tc.function.name,
                        args=dict(tc.function.arguments) if tc.function.arguments else {},
                    )
                )

        out = Message(
            role=Role.ASSISTANT,
            content=msg.content or "",
            agent=self.name,
            tool_calls=tool_calls,
        )
        out._trace_note = trace_note  # picked up by orchestrator
        return out

    def _to_ollama(self, messages: list[Message]) -> list[dict]:
        result: list[dict] = []

        # Merge static system_prompt + all injected Role.SYSTEM messages (memory,
        # language hints, skills) into one leading system block so the model sees
        # the full context. Formerly these were silently dropped.
        system_parts = []
        if self.system_prompt:
            system_parts.append(self.system_prompt)
        for m in messages:
            if m.role == Role.SYSTEM:
                system_parts.append(m.content)
        if system_parts:
            result.append({"role": "system", "content": "\n\n".join(system_parts)})

        for m in messages:
            if m.role == Role.SYSTEM:
                continue
            elif m.role == Role.TOOL:
                # Link the result to the call by tool name. Ollama's chat template
                # attaches a `tool` message to the preceding assistant tool_calls
                # turn; without that turn (and this name) it drops the result, so
                # the model never sees it and re-issues the same call every round.
                entry = {"role": "tool", "content": m.content}
                if m.tool_name:
                    entry["tool_name"] = m.tool_name
                result.append(entry)
            elif m.role == Role.ASSISTANT and m.tool_calls:
                # Preserve the assistant's tool-call turn (Ollama shape) so the
                # tool results that follow are properly anchored to it.
                result.append({
                    "role": "assistant",
                    "content": m.content or "",
                    "tool_calls": [
                        {"function": {"name": tc.name, "arguments": tc.args}}
                        for tc in m.tool_calls
                    ],
                })
            else:
                result.append({"role": m.role.value, "content": m.content})
        return result
