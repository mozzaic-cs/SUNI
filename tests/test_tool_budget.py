"""The tool list has to fit the window it is sent through.

Measured on the machine where this was found: role "standard" is offered 61
tools, ~7,100 tokens against a num_ctx of 8,192 — 87% of the window gone before
the persona, the memory injection, the skills catalogue or the message. The
prompt overflowed, Ollama truncated it, and a 7B handed a truncated tool schema
generated until it hit the context limit. Every inbound channel message met
that, for as long as the channel had existed, because nothing else uses the
local path.
"""
from __future__ import annotations

import importlib
import json
import pkgutil

import pytest

from suni.core.orchestrator import _schema_tokens, fit_tools


def _real_tools(role="standard"):
    """Every schema a role is actually offered — the real ones, not fixtures.

    The whole finding is about a real total against a real window, so a test
    with six invented tools would prove nothing about it.
    """
    from suni import rbac
    import suni.tools as T

    blocked = set(rbac.blocked_tools(role))
    out = []
    for m in pkgutil.iter_modules(T.__path__):
        try:
            mod = importlib.import_module("suni.tools." + m.name)
        except Exception:
            continue
        for attr in dir(mod):
            if attr == "SCHEMA" or attr.endswith("_SCHEMA"):
                s = getattr(mod, attr)
                if isinstance(s, dict) and "name" in s and s["name"] not in blocked:
                    out.append({"type": "function", "function": s})
    return out


def test_the_full_tool_set_really_does_not_fit_a_small_window():
    """The premise. If this ever stops being true the trimming is dead code and
    should be questioned rather than quietly kept."""
    tools = _real_tools()
    total = sum(_schema_tokens(t) for t in tools)
    assert len(tools) > 40, f"only {len(tools)} tools — has the registry shrunk?"
    assert total > 8192 * 0.6, (
        f"{total} tokens now fits comfortably in 8192; the budget may be moot")


@pytest.mark.parametrize("ctx", [32768, 65536, 131072])
def test_a_big_window_is_left_completely_alone(ctx):
    """This is arithmetic about a window, not a rule that local models get
    fewer tools. Hardware with room must send everything, untouched."""
    tools = _real_tools()
    kept, dropped = fit_tools(tools, "anything at all", ctx)
    assert dropped == 0, f"{dropped} tools dropped at num_ctx={ctx}"
    assert kept == tools, "the list was reordered even though it all fit"


def test_a_small_window_is_cut_to_fit_with_room_to_spare():
    tools = _real_tools()
    kept, dropped = fit_tools(tools, "hello", 8192)
    used = sum(_schema_tokens(t) for t in kept)
    assert dropped > 0, "nothing was trimmed on a window that cannot hold it"
    assert used <= 8192 * 0.5, (
        f"{used} tokens still leaves nothing for the persona, memory or reply")


def test_what_was_asked_for_survives_the_cut():
    """A tool nobody could have wanted is the right one to drop; the one the
    message is plainly about is not."""
    tools = _real_tools()
    kept, _ = fit_tools(tools, "search the web for the weather in Lisbon", 8192)
    names = [t["function"]["name"] for t in kept]
    assert "web_search" in names, "the obviously relevant tool was cut"


def test_the_core_set_survives_whatever_was_asked():
    """A conversation without memory is not much of one, however irrelevant the
    words of one particular message happen to be."""
    from suni import config

    tools = _real_tools()
    core = tuple(config.get("tool_core") or ())
    assert core, "no core set configured"
    kept, _ = fit_tools(tools, "zzzz qqqq", 4096, core=core)
    names = {t["function"]["name"] for t in kept}
    offered = {t["function"]["name"] for t in tools}
    for c in core:
        if c in offered:
            assert c in names, f"core tool {c!r} was dropped"


def test_the_core_names_are_real():
    """A core list naming tools that do not exist protects nothing, and nothing
    would ever say so."""
    from suni import config

    offered = {t["function"]["name"] for t in _real_tools("admin")}
    for c in (config.get("tool_core") or []):
        assert c in offered, f"core tool {c!r} is not a registered tool"


def test_it_is_wired_into_the_request_path():
    """A budget that is never called is a budget that never budgets."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "suni/core/orchestrator.py").read_text(encoding="utf-8")
    assert "tools, _dropped = fit_tools(" in src, "nothing calls it"
    assert "effective_num_ctx" in src, "the budget is not sized to the real window"
    assert "[TOOLS]" in src, "a silent trim is how this went unnoticed for months"
