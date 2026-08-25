from __future__ import annotations
import asyncio
import inspect
import time
from contextvars import ContextVar
from typing import Any, Callable
from ..logger import get_logger

_log = get_logger(__name__)

# Set by Orchestrator.run() for each request; read by execute() to inject
# _-prefixed system parameters into tool handlers (e.g. _user_id).
USER_ID_CTX: ContextVar[str] = ContextVar("user_id", default="")

# Parameters the registry can inject into handlers that declare them.
# Handlers opt in by declaring the param with a leading underscore.
_INJECTABLE = ("_user_id",)


class ToolRegistry:
    def __init__(self):
        self._schemas: dict[str, dict] = {}
        self._handlers: dict[str, Callable] = {}
        self._mcp_prefixes: list[str] = []

    def register(self, schema: dict, handler: Callable) -> None:
        name = schema["name"]
        self._schemas[name] = schema
        self._handlers[name] = handler

    def _strip_unknown_args(self, name: str, handler, merged: dict) -> set[str]:
        """Remove arguments the handler cannot accept. Returns what was removed.

        The schema is the source of truth where it declares properties, because
        that is what the model was shown. The handler signature is the fallback
        and the backstop — a schema can drift from its function, and it is the
        function that raises.

        A handler taking **kwargs is left alone: it opted into whatever arrives.
        """
        import inspect
        try:
            sig = inspect.signature(handler)
        except (TypeError, ValueError):
            return set()
        if any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in sig.parameters.values()):
            return set()

        accepted = set(sig.parameters)
        schema = self._schemas.get(name) or {}
        declared = set((schema.get("parameters") or {}).get("properties") or {})
        if declared:
            # Underscore-prefixed parameters are injected by the runtime
            # (_user_id and friends) and are deliberately absent from the
            # schema, so intersecting with `declared` alone would discard them.
            system = {p for p in sig.parameters if p.startswith("_")}
            accepted &= declared | system
        unknown = {k for k in merged if k not in accepted}
        for k in unknown:
            merged.pop(k, None)
        if unknown:
            _log.warning("[TOOL] %s: ignoring unknown argument(s) %s",
                         name, ", ".join(sorted(unknown)))
        return unknown

    async def execute(self, name: str, args: dict) -> Any:
        if name not in self._handlers:
            msg = f"tool '{name}' not found"
            _log.error("[TOOL_FAIL] %s: %s", name, msg)
            return f"Error: {msg}. Available: {list(self._handlers)}"
        t0 = time.perf_counter()
        try:
            handler = self._handlers[name]
            merged = dict(args)
            # A small model folds arguments from the wider request into
            # whichever tool it calls first — "make a PDF and email it to X"
            # produced create_pdf(content=…, path=…, to="X"). Passing that
            # straight through raised TypeError: unexpected keyword argument
            # 'to', the raw Python error went back as the tool result, and the
            # model answered the user with a lecture about JSON formatting
            # instead of retrying. Dropping the stray argument lets the call
            # succeed; the model is told what was ignored so it can make the
            # follow-up call it actually needed.
            dropped = self._strip_unknown_args(name, handler, merged)
            _inject_system_params(handler, merged)
            if asyncio.iscoroutinefunction(handler):
                result = await handler(**merged)
            else:
                result = handler(**merged)
            elapsed = time.perf_counter() - t0
            result_str = str(result)
            preview = result_str[:120] + ("…" if len(result_str) > 120 else "")
            _log.info("[TOOL] %-30s %.2fs  %s", name, elapsed, preview)
            if dropped:
                # Appended rather than logged only: the model needs to know the
                # part of its intent that did not happen, or the email never
                # gets sent and nothing says so.
                return (f"{result}\n[note] {name} ignored these arguments: "
                        f"{', '.join(sorted(dropped))}. If they were meant for "
                        f"another tool, call that tool now.")
            return result
        except Exception as e:
            elapsed = time.perf_counter() - t0
            _log.error("[TOOL_FAIL] %s (%.2fs): %s", name, elapsed, e, exc_info=True)
            return f"Tool error ({name}): {e}"

    def get_ollama_tools(
        self,
        include_prefixes: list[str] | None = None,
        allowed_tools:   list[str] | None = None,
        blocked_tools:   list[str] | None = None,
    ) -> list[dict]:
        """Format for Ollama's tool calling API.

        include_prefixes: if set, MCP-prefixed tools (e.g. 'playwright_', 'filesystem_')
        are only included when their prefix appears in the list. Non-prefixed tools are
        always included. Pass None to include everything.
        """
        _blocked = set(blocked_tools or [])
        result = []
        for s in self._schemas.values():
            name = s["name"]
            mcp_prefix = next(
                (p for p in (self._mcp_prefixes or []) if name.startswith(p + "_")),
                None,
            )
            if mcp_prefix is None:
                # Non-MCP tool — apply allowed/blocked role filters
                if name in _blocked:
                    continue
                if allowed_tools is not None and name not in allowed_tools:
                    continue
                result.append({"type": "function", "function": s})
            elif include_prefixes is None or mcp_prefix in include_prefixes:
                # MCP tool — only blocked list applies (no allowlist for MCP)
                if name not in _blocked:
                    result.append({"type": "function", "function": s})
        return result

    def clear_mcp_tools(self) -> int:
        """Remove all tools registered under any known MCP prefix. Returns count."""
        to_remove = [
            name for name in list(self._schemas)
            if any(name.startswith(p + "_") for p in self._mcp_prefixes)
        ]
        for name in to_remove:
            self._schemas.pop(name, None)
            self._handlers.pop(name, None)
        return len(to_remove)

    def register_mcp_prefix(self, prefix: str) -> None:
        """Mark a server prefix so its tools can be filtered selectively."""
        if not hasattr(self, "_mcp_prefixes") or self._mcp_prefixes is None:
            self._mcp_prefixes = []
        if prefix not in self._mcp_prefixes:
            self._mcp_prefixes.append(prefix)

    def is_mcp_tool(self, name: str) -> bool:
        """True if this tool came from an MCP server rather than SUNI itself.

        Needed by the approval gate: MCP tools are named "{server}_{tool}" and
        so can never match its static list of consequential SUNI tools, which
        meant every one of them — including shell execution and payments —
        skipped approval entirely.
        """
        return any(name.startswith(p + "_") for p in (self._mcp_prefixes or []))

    def mcp_prefix_of(self, name: str) -> str | None:
        """The server a tool came from, or None if it is a native SUNI tool."""
        return next((p for p in (self._mcp_prefixes or [])
                     if name.startswith(p + "_")), None)

    def get_claude_tools(self) -> list[dict]:
        """Format for Anthropic's tool calling API."""
        return [
            {
                "name": s["name"],
                "description": s["description"],
                "input_schema": s["parameters"],
            }
            for s in self._schemas.values()
        ]

    def names(self) -> list[str]:
        return list(self._schemas.keys())

    def __len__(self) -> int:
        return len(self._schemas)


def _inject_system_params(handler: Callable, args: dict) -> None:
    """
    Inspect the handler signature and fill any _-prefixed system parameters
    that the handler declares but the LLM-generated args dict doesn't contain.
    Skips VAR_KEYWORD / VAR_POSITIONAL parameters.
    """
    available = {"_user_id": USER_ID_CTX.get()}
    try:
        sig = inspect.signature(handler)
    except (ValueError, TypeError):
        return
    for pname, param in sig.parameters.items():
        if (
            pname in _INJECTABLE
            and pname not in args
            and param.kind not in (param.VAR_KEYWORD, param.VAR_POSITIONAL)
        ):
            args[pname] = available[pname]
