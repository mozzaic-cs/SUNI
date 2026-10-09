"""SUNI's own tools, lent to the Claude Code CLI for one turn.

With force_claude_code on, every turn goes to the CLI, and the CLI has only its
own tools. Anything only SUNI can do — look at the screen, draw an image, send
mail, start a coding run in a project — stopped working the day that switch was
turned on, while still working for anyone on a local model. Measured
2026-10-09: "analisa o site que tenho aberto no ecrã principal" was answered
"a captura de ecrã foi bloqueada nesta sessão", because the screenshot tool was
never offered. What SUNI can do should not depend on which model answers.

So each Claude Code turn gets an MCP endpoint (/mcp/bridge on SUNI's own server)
and a token that opens it for that turn only. Every call goes through the same
gate as the local tool loop — role, policy, the security review, a person's
approval, the output guard (Orchestrator._gated_call) — and runs inside the
context of the turn that asked, so a tool sees the same user, output folder and
event stream it would have seen from the local loop.

What it is not:
  - Not every tool. A short list of things only SUNI has. Lending SUNI's
    write_file or run_shell would open an edit path that skips code_task's
    per-run approval and folder confinement, and the CLI has its own readers.
  - Not a nested coding run. start_code_run records the request; the run starts
    after the CLI has finished its reply, with its own approval card. Run from
    inside the call, it would be a second CLI session racing the first one's
    timeout.
  - Not a credential. The token opens /mcp/bridge and nothing else — not
    /api/chat and not approvals — and it dies with the turn.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger("suni.tool_bridge")

SERVER_NAME = "suni_bridge"
CODE_RUN = "start_code_run"
# Only tools the CLI has no equivalent for. See the module docstring.
BRIDGE_TOOLS = ("look_at_screen", "generate_image", "send_email")
# The answer to "is anything waiting on a person?" must arrive before the CLI
# that asked is killed, or the card approves a call nobody is waiting for.
_APPROVAL_MARGIN_S = 15

PROTOCOL_VERSION = "2025-06-18"

_ROOT = Path(__file__).resolve().parents[2]
_CERT = _ROOT / "certs" / "cert.pem"
_KEY = _ROOT / "certs" / "key.pem"

# The turn the CLI is about to run for, set by the orchestrator around the call.
CURRENT: contextvars.ContextVar["Turn | None"] = contextvars.ContextVar(
    "suni_tool_bridge_turn", default=None)


@dataclass
class Turn:
    token: str
    orch: Any
    user_id: str
    user_role: str
    conv_mode: str
    grants: dict | None
    user_input: str
    event_cb: Any
    ctx: contextvars.Context
    deadline: float                       # time.monotonic()
    handoff: dict | None = None
    tools: list[str] = field(default_factory=list)


_live: dict[str, Turn] = {}


def open_turn(orch, *, user_id: str, user_role: str, conv_mode: str,
              grants: dict | None, user_input: str, event_cb,
              timeout_s: int) -> Turn | None:
    """A token for this turn, or None when there is nothing to lend.

    Needs a live event stream: every tool here is approval-gated, and without
    one nobody would see the card."""
    if event_cb is None or conv_mode == "read-only":
        return None
    t = Turn(token=secrets.token_urlsafe(32), orch=orch, user_id=user_id,
             user_role=user_role, conv_mode=conv_mode, grants=grants,
             user_input=user_input, event_cb=event_cb,
             ctx=contextvars.copy_context(),
             deadline=time.monotonic() + max(30, timeout_s - _APPROVAL_MARGIN_S))
    t.tools = _visible(t)
    if not t.tools:
        return None
    _live[t.token] = t
    return t


def close_turn(turn: Turn | None) -> None:
    if turn is not None:
        _live.pop(turn.token, None)


def lookup(token: str) -> Turn | None:
    t = _live.get(token or "")
    if t is not None and time.monotonic() > t.deadline + _APPROVAL_MARGIN_S:
        # The CLI is gone by now whatever happened; do not leave a door open.
        _live.pop(token, None)
        return None
    return t


def _code_run_allowed(t: Turn, allowed, blocked) -> bool:
    from ..tools import code_project as _cp
    return ("code_task" in t.orch.registry.names()
            and "code_task" not in (blocked or [])
            and (allowed is None or "code_task" in allowed)
            and bool(_cp._roots()))


def _visible(t: Turn) -> list[str]:
    """The bridge tools this caller may use, by the same rule as the local loop."""
    allowed, blocked = t.orch._tool_permissions(t.user_role, t.conv_mode, t.grants)
    names = set(t.orch.registry.names())
    out = [n for n in BRIDGE_TOOLS
           if n in names and n not in (blocked or [])
           and (allowed is None or n in allowed)]
    if _code_run_allowed(t, allowed, blocked):
        out.append(CODE_RUN)
    return out


_CODE_RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "project_dir": {"type": "string",
                        "description": "The project folder, e.g. C:\\inetpub\\wwwroot\\isoblend."},
        "task": {"type": "string",
                 "description": "What to change, complete enough to act on without this conversation."},
    },
    "required": ["project_dir", "task"],
}


def _schema(t: Turn, name: str) -> dict:
    if name == CODE_RUN:
        from ..tools import code_project as _cp
        roots = ", ".join(_cp._roots())
        return {"name": CODE_RUN,
                "description": (
                    "Change files in a code project. You cannot write files in "
                    "this session; this hands the change to a coding run in that "
                    "folder, which starts right after your reply once the user "
                    f"approves it on screen. Allowed folders: {roots}."),
                "inputSchema": _CODE_RUN_SCHEMA}
    s = t.orch.registry._schemas[name]
    return {"name": name, "description": s.get("description", ""),
            "inputSchema": s.get("parameters") or {"type": "object", "properties": {}}}


async def call_tool(t: Turn, name: str, args: dict) -> tuple[str, bool]:
    """(text, is_error)."""
    if name not in t.tools:
        return f"No tool named {name!r} here.", True
    if not isinstance(args, dict):
        return "Arguments must be an object.", True
    if name == CODE_RUN:
        return _queue_code_run(t, args)
    if t.event_cb:
        t.event_cb({"type": "tool_start", "name": name})
    # The turn's own context, copied per call: the tool sees the user, output
    # folder and event stream of the request that asked, and two calls in
    # flight never share one.
    task = asyncio.get_running_loop().create_task(
        t.orch._gated_call(name, args, user_id=t.user_id, user_role=t.user_role,
                           user_input=t.user_input, event_cb=t.event_cb,
                           approval_deadline=t.deadline),
        context=t.ctx.copy())
    result, ran, elapsed_ms = await task
    result = str(result)
    if ran and t.event_cb:
        from .orchestrator import _is_tool_failure
        t.event_cb({"type": "tool_end", "name": name, "result": result[:200],
                    "elapsed_ms": elapsed_ms, "ok": not _is_tool_failure(result)})
    _log.info("[BRIDGE] %s for %s → %s", name, t.user_id, "ran" if ran else "stopped")
    return result, not ran


def _queue_code_run(t: Turn, args: dict) -> tuple[str, bool]:
    from ..tools import code_project as _cp
    task = str(args.get("task") or "").strip()
    if not task:
        return "Say what to change.", True
    real, why = _cp.resolve_project(str(args.get("project_dir") or ""))
    if not real:
        return why, True
    if t.handoff:
        return (f"A coding run in {t.handoff['project_dir']} is already queued "
                "for this reply. Put everything into that one."), True
    t.handoff = {"project_dir": real, "task": task}
    _log.info("[BRIDGE] coding run queued in %s for %s", real, t.user_id)
    return (f"Queued. A coding run in {real} starts right after your reply, once "
            "the user approves it on screen. Do not attempt the change yourself. "
            "Tell them in one short sentence that you are handing it to a coding "
            "run and what it will do, then stop."), False


async def handle_rpc(t: Turn, msg: Any) -> dict | None:
    """One JSON-RPC message from the CLI; None for a notification."""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Batch requests are not supported."}}
    mid = msg.get("id")
    if mid is None:
        return None
    method = msg.get("method")
    params = msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "suni", "version": "1"}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [_schema(t, n) for n in t.tools]})
    if method == "tools/call":
        try:
            text, is_err = await call_tool(t, str(params.get("name") or ""),
                                           params.get("arguments") or {})
        except Exception as exc:      # noqa: BLE001 — the CLI must get an answer
            _log.warning("[BRIDGE] %s failed: %s", params.get("name"), exc, exc_info=True)
            text, is_err = f"The tool failed: {exc}", True
        return ok({"content": [{"type": "text", "text": text}], "isError": is_err})
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}


# ── what the CLI is started with ─────────────────────────────────────────────

def base_url() -> str:
    # web.py serves TLS whenever both files exist, so the same test here.
    port = int(os.environ.get("SUNI_PORT", 8765))
    scheme = "https" if (_CERT.exists() and _KEY.exists()) else "http"
    return f"{scheme}://localhost:{port}/mcp/bridge"


def write_mcp_config(t: Turn) -> str:
    """A temp file for --mcp-config. The token is a header, never an argument:
    a command line is visible to every process on the machine."""
    cfg = {"mcpServers": {SERVER_NAME: {
        "type": "http", "url": base_url(),
        "headers": {"Authorization": f"Bearer {t.token}"}}}}
    fd, path = tempfile.mkstemp(suffix=".json", prefix="suni_bridge_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


def allowed_tools(t: Turn) -> list[str]:
    return [f"mcp__{SERVER_NAME}__{n}" for n in t.tools]


def cli_env(timeout_s: int) -> dict[str, str]:
    env = {"MCP_TOOL_TIMEOUT": str(int(timeout_s) * 1000)}
    if _CERT.exists():
        # SUNI's certificate is self-signed. Without this the CLI cannot
        # connect, and says nothing: the tools are simply absent (measured).
        env["NODE_EXTRA_CA_CERTS"] = str(_CERT)
    return env


def task_note(t: Turn) -> str:
    """Appended to the CLI's task so it knows what it has been lent."""
    lines = ["[SUNI's own tools are available to you this turn as "
             f"mcp__{SERVER_NAME}__*. Use them instead of saying you cannot:"]
    if "look_at_screen" in t.tools:
        lines.append("- look_at_screen: when they ask about something on their "
                     "screen. 'main/principal screen' is monitor='primary'. It "
                     "returns image paths; open them with Read.")
    if "generate_image" in t.tools:
        lines.append("- generate_image: when they ask for a picture to be made.")
    if "send_email" in t.tools:
        lines.append("- send_email: when they ask for mail to be sent.")
    if CODE_RUN in t.tools:
        lines.append(f"- {CODE_RUN}: you cannot write files here. When they ask "
                     "for changes to files in a project, call it instead of "
                     "trying to edit.")
    lines.append("Each needs the user's approval on screen; if it is denied, "
                 "say so and do not try another way.]")
    return "\n".join(lines)
