"""
Tools that exist must be registered, and agents must not silently reference
tools that are not.

The bug that prompted this: an agent declared ping_host, the grant intersection
kept the name, the registry returned nothing for it because network_tool was
registered in the CLI but not in the web server, and the model — handed an empty
tool list — answered with the literal text ping_host("localhost").

That reads as a weak model. It was a missing registration. Two guards follow:
the modules are registered, and a profile naming a tool that does not exist says
so instead of quietly having no tools.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from suni import agents

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _registered(path: pathlib.Path, encoding: str) -> set[str]:
    src = path.read_text(encoding=encoding)
    i = src.index("registry.register")
    return {m for m, _ in re.findall(r"registry\.register\(\s*(\w+)\.(\w+)", src[i - 200:i + 9000])}


@pytest.fixture(scope="module")
def server_mods() -> set[str]:
    return _registered(ROOT / "suni" / "web" / "server.py", "utf-8-sig")


@pytest.mark.parametrize("module", ["network_tool", "memory_tool"])
def test_tools_the_cli_has_are_registered_in_the_server_too(server_mods, module):
    """The web UI is the primary surface; a tool only the CLI has is invisible
    to almost every user. memory_tool is the sharper case — run() binds it every
    request, which does nothing at all if it is not registered."""
    assert module in server_mods, f"{module} is not registered in the web server"


def test_the_memory_binding_is_not_pointless():
    """run() calls memory_tool.bind() per request. If the memory tools are not
    registered, that binding maintains state for tools nobody can call."""
    orch = (ROOT / "suni" / "core" / "orchestrator.py").read_text(encoding="utf-8")
    assert "_memory_tool.bind" in orch
    srv = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    assert "SEARCH_SCHEMA" in srv and "memory_tool" in srv


# ── declaring a tool that does not exist ─────────────────────────────────────
def test_unknown_tools_are_reported():
    a = {"tools": ["web_search", "ping_host", "not_a_real_tool"]}
    assert agents.unknown_tools(a, ["web_search", "ping_host"]) == ["not_a_real_tool"]


def test_nothing_reported_when_every_tool_exists():
    a = {"tools": ["web_search"]}
    assert agents.unknown_tools(a, ["web_search", "ping_host"]) == []


def test_inheriting_agents_report_nothing():
    """tools=None means 'whatever the caller may use' — there is nothing to check."""
    assert agents.unknown_tools({"tools": None}, ["web_search"]) == []


def test_an_unknown_registry_produces_no_false_warnings():
    """A caller that cannot enumerate tools must not generate noise."""
    assert agents.unknown_tools({"tools": ["anything"]}, None) == []


def test_the_orchestrator_warns_rather_than_failing_the_turn():
    orch = (ROOT / "suni" / "core" / "orchestrator.py").read_text(encoding="utf-8")
    i = orch.index("unknown_tools as _unk")
    block = orch[i - 400:i + 900]
    assert "_log.warning" in block
    assert "agent_warning" in block, "nothing surfaces to the UI"
    assert "except Exception" in block, "a warning could fail the request"
