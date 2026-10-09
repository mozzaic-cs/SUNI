"""SUNI's own tools must reach a turn answered by the Claude Code CLI.

With force_claude_code on, every turn went to the CLI and only the CLI's own
tools existed: "analisa o site que tenho aberto no ecrã principal" was answered
"a captura de ecrã foi bloqueada nesta sessão" (2026-10-09), and an edit in a
configured project ended in refused writes. These turns go through the real
Orchestrator.run with a stand-in for the CLI that does what the CLI does over
MCP — list the tools, call one — so the bridge, the gate and the handoff are
all the real ones.
"""
import asyncio
import time

import pytest

from suni import approval
from suni.core import tool_bridge as tb
from suni.core.context import Context
from suni.core.message import Message, Role
from suni.core.model_tier import CLAUDE_CODE_TIER
from suni.core.orchestrator import Orchestrator
from suni.tools import code_project as cp
from suni.tools import screen_tool
from suni.tools.registry import ToolRegistry, USER_ID_CTX


class _NoModel:
    name = "must-not-be-called"

    async def chat(self, *a, **k):
        raise AssertionError("a local model was asked; force_claude_code is on")


class _FakeCLI:
    """Plays the CLI: whatever `script` does with the turn is what it 'did'."""
    name = "claude-code"

    def __init__(self, script):
        self.script = script
        self.seen = None

    async def chat(self, messages, context, tools=None):
        turn = tb.CURRENT.get()
        self.seen = turn
        text = await self.script(turn) if turn else "no bridge"
        return Message(role=Role.ASSISTANT, content=text, agent=self.name)


@pytest.fixture
def env(monkeypatch, tmp_path):
    root = tmp_path / "wwwroot"
    (root / "isoblend").mkdir(parents=True)
    real_get = cp._cfg.get
    values = {"code_project_roots": [str(root)], "force_claude_code": True,
              "claude_code_timeout": 300, "intent_judge": False}
    monkeypatch.setattr(cp._cfg, "get",
                        lambda k, d=None: values[k] if k in values else real_get(k, d))

    asked = []

    async def answer(tool, args, user_id, event_cb, risk=None):
        asked.append(tool)
        return values.get("_decision", "allow")
    monkeypatch.setattr(approval, "request_approval", answer)

    shots, runs = [], []

    async def fake_screen(reason="", monitor="active"):
        # The user is read from the context, as the real tool does — this is
        # what proves the call ran in the turn's own context.
        shots.append((USER_ID_CTX.get(""), monitor))
        return "Captured 1 screen(s): screen 1 — C:\\out\\screen_1.png"

    async def fake_code_task(**args):
        runs.append(args)
        return "Claude Code finished.\n\nChanged the dashboard.\n\n[code session: s-1]"

    reg = ToolRegistry()
    reg.register(screen_tool.SCHEMA, fake_screen)
    reg.register(cp.SCHEMA, fake_code_task)
    return {"root": root, "values": values, "asked": asked, "shots": shots,
            "runs": runs, "reg": reg}


def _run(env, script, text="analisa o site que tenho aberto no ecrã principal",
         *, role="admin", mode="assistant", event_cb=True):
    cli = _FakeCLI(script)
    orch = Orchestrator(_NoModel(), env["reg"], tier_agents={CLAUDE_CODE_TIER: cli})
    events = []
    token = USER_ID_CTX.set("u1")
    try:
        out = asyncio.run(orch.run(text, Context(), user_role=role, conv_mode=mode,
                                   event_cb=events.append if event_cb else None,
                                   user_id="u1"))
    finally:
        USER_ID_CTX.reset(token)
    return out, cli, events


def test_the_screen_is_reachable_on_the_cli_path(env):
    async def script(turn):
        names = [t["name"] for t in (await tb.handle_rpc(
            turn, {"id": 1, "method": "tools/list"}))["result"]["tools"]]
        assert "look_at_screen" in names
        r = await tb.handle_rpc(turn, {"id": 2, "method": "tools/call", "params": {
            "name": "look_at_screen", "arguments": {"reason": "layout", "monitor": "primary"}}})
        assert r["result"]["isError"] is False
        return "Vi o painel."

    out, cli, events = _run(env, script)
    assert out == "Vi o painel."
    assert env["shots"] == [("u1", "primary")]
    assert env["asked"] == ["look_at_screen"], "the screen must still be approved"
    assert any(e.get("type") == "tool_start" and e.get("name") == "look_at_screen"
               for e in events)


def test_a_denied_capture_does_not_run(env):
    env["values"]["_decision"] = "deny"

    async def script(turn):
        r = await tb.handle_rpc(turn, {"id": 1, "method": "tools/call", "params": {
            "name": "look_at_screen", "arguments": {"reason": "x"}}})
        assert r["result"]["isError"] is True
        assert "denied" in r["result"]["content"][0]["text"]
        return "ok"

    _run(env, script)
    assert env["shots"] == []


def test_the_token_dies_with_the_turn(env):
    async def script(turn):
        assert tb.lookup(turn.token) is turn
        return "ok"

    _, cli, _ = _run(env, script)
    assert cli.seen is not None
    assert tb.lookup(cli.seen.token) is None
    assert tb.lookup("") is None and tb.lookup("guess") is None


@pytest.mark.parametrize("mode,cb", [("read-only", True), ("assistant", False)])
def test_no_bridge_in_read_only_mode_or_without_a_live_stream(env, mode, cb):
    # Every lent tool is approval-gated: without a live stream nobody sees
    # the card, and read-only mode lends nothing that acts.
    orch = Orchestrator(_NoModel(), env["reg"])
    assert tb.open_turn(orch, user_id="u1", user_role="admin", conv_mode=mode,
                        grants=None, user_input="x",
                        event_cb=(lambda e: None) if cb else None, timeout_s=300) is None


def test_only_the_short_list_is_lent(env):
    async def write_file(path="", content=""):
        raise AssertionError("never lent")
    env["reg"].register({"name": "write_file", "description": "w",
                         "parameters": {"type": "object", "properties": {}}}, write_file)

    async def script(turn):
        assert "write_file" not in turn.tools
        r = await tb.handle_rpc(turn, {"id": 1, "method": "tools/call", "params": {
            "name": "write_file", "arguments": {"path": "x"}}})
        assert r["result"]["isError"] is True
        return "ok"

    _run(env, script)


def test_a_role_without_the_tool_does_not_get_it(env, monkeypatch):
    orch = Orchestrator(_NoModel(), env["reg"])
    monkeypatch.setattr(orch, "_tool_permissions",
                        lambda role, mode, grants: (None, ["look_at_screen"]))
    t = tb.open_turn(orch, user_id="u1", user_role="admin", conv_mode="assistant",
                     grants=None, user_input="x", event_cb=lambda e: None, timeout_s=300)
    try:
        assert t is not None and "look_at_screen" not in t.tools
    finally:
        tb.close_turn(t)


def test_an_edit_request_becomes_an_approved_coding_run(env):
    proj = str(env["root"] / "isoblend")

    async def script(turn):
        assert tb.CODE_RUN in turn.tools
        r = await tb.handle_rpc(turn, {"id": 1, "method": "tools/call", "params": {
            "name": tb.CODE_RUN,
            "arguments": {"project_dir": proj, "task": "max width 1720px on the dashboard"}}})
        assert r["result"]["isError"] is False
        assert env["runs"] == [], "the run must not start inside the CLI's call"
        return "Vou passar isto a uma execução de código."

    out, _, _ = _run(env, script, "ok mostra-me como fica")
    assert len(env["runs"]) == 1
    assert env["runs"][0]["project_dir"].lower() == proj.lower()
    assert env["runs"][0]["task"] == "max width 1720px on the dashboard"
    assert env["asked"] == ["code_task"], "the coding run gets its own card"
    assert out.startswith("Vou passar isto") and "Changed the dashboard." in out
    assert "code session" not in out


def test_a_coding_run_outside_the_roots_is_refused(env, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()

    async def script(turn):
        r = await tb.handle_rpc(turn, {"id": 1, "method": "tools/call", "params": {
            "name": tb.CODE_RUN, "arguments": {"project_dir": str(other), "task": "x"}}})
        assert r["result"]["isError"] is True
        assert "outside" in r["result"]["content"][0]["text"]
        return "ok"

    _run(env, script, "muda isto")
    assert env["runs"] == [] and env["asked"] == []


def test_an_unanswered_card_is_a_deny_before_the_cli_dies(env, monkeypatch):
    async def never(tool, args, user_id, event_cb, risk=None):
        await asyncio.sleep(3600)
    monkeypatch.setattr(approval, "request_approval", never)

    async def script(turn):
        turn.deadline = time.monotonic() + 0.05     # as if the CLI's time were up
        r = await tb.handle_rpc(turn, {"id": 1, "method": "tools/call", "params": {
            "name": "look_at_screen", "arguments": {"reason": "x"}}})
        assert r["result"]["isError"] is True
        return "ok"

    t0 = time.monotonic()
    _run(env, script)
    assert time.monotonic() - t0 < 10
    assert env["shots"] == []


def test_the_cli_is_started_with_the_bridge(env, monkeypatch):
    """The real ClaudeCodeAgent, with the subprocess replaced: the flags, the
    environment and the config file are what the measured spike needed."""
    from suni.models import claude_code_agent as cca
    from suni.tools import claude_code_advanced as cca_adv
    import json
    import os

    seen = {}

    async def fake_stream(args, on_line, timeout=300, cwd=None, stdin_data=None,
                          extra_env=None):
        seen["args"], seen["env"], seen["task"] = list(args), dict(extra_env or {}), stdin_data
        cfg_path = args[args.index("--mcp-config") + 1]
        seen["cfg"] = json.load(open(cfg_path, encoding="utf-8"))
        seen["cfg_path"] = cfg_path
        on_line(json.dumps({"type": "result", "result": "feito", "session_id": "s"}))
        return 0, "", ""
    monkeypatch.setattr(cca_adv, "_run_claude_stream", fake_stream)

    orch = Orchestrator(_NoModel(), env["reg"],
                        tier_agents={CLAUDE_CODE_TIER: cca.ClaudeCodeAgent()})
    events = []
    out = asyncio.run(orch.run("analisa o ecrã principal", Context(), user_role="admin",
                               conv_mode="assistant", event_cb=events.append, user_id="u1"))
    assert out == "feito"
    tools = seen["args"][seen["args"].index("--allowedTools") + 1].split(",")
    assert "mcp__suni_bridge__look_at_screen" in tools and "Read" in tools
    assert not any(t in ("Write", "Edit") for t in tools)
    srv = seen["cfg"]["mcpServers"]["suni_bridge"]
    assert srv["url"].endswith("/mcp/bridge")
    token = srv["headers"]["Authorization"].split(" ", 1)[1]
    assert token not in " ".join(seen["args"]), "the token must not be on the command line"
    assert tb.lookup(token) is None, "revoked once the turn ended"
    assert not os.path.exists(seen["cfg_path"]), "config file removed"
    assert "MCP_TOOL_TIMEOUT" in seen["env"]
    assert "look_at_screen" in seen["task"]


def test_rpc_basics():
    class _Orch:
        class registry:
            _schemas = {}
    t = tb.Turn(token="t", orch=_Orch, user_id="u", user_role="admin",
                conv_mode="assistant", grants=None, user_input="", event_cb=None,
                ctx=None, deadline=time.monotonic() + 60, tools=[])
    loop = asyncio.new_event_loop()
    try:
        init = loop.run_until_complete(tb.handle_rpc(t, {"id": 1, "method": "initialize",
                                                         "params": {"protocolVersion": "2025-06-18"}}))
        assert init["result"]["capabilities"] == {"tools": {}}
        assert loop.run_until_complete(tb.handle_rpc(t, {"method": "notifications/initialized"})) is None
        assert loop.run_until_complete(tb.handle_rpc(t, {"id": 2, "method": "nope"}))["error"]["code"] == -32601
        assert loop.run_until_complete(tb.handle_rpc(t, [1]))["error"]["code"] == -32600
    finally:
        loop.close()
