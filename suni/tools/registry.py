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

    async def execute(self, name: str, args: dict) -> Any:
        if name not in self._handlers:
            msg = f"tool '{name}' not found"
            _log.error("[TOOL_FAIL] %s: %s", name, msg)
            return f"Error: {msg}. Available: {list(self._handlers)}"
        t0 = time.perf_counter()
        try:
            handler = self._handlers[name]
            merged = dict(args)
            _inject_system_params(handler, merged)
            if asyncio.iscoroutinefunction(handler):
                result = await handler(**merged)
            else:
                result = handler(**merged)
            elapsed = time.perf_counter() - t0
            result_str = str(result)
            preview = result_str[:120] + ("…" if len(result_str) > 120 else "")
            _log.info("[TOOL] %-30s %.2fs  %s", name, elapsed, preview)
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
