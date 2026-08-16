"""
MCP tools must pass through the approval gate.

The gate matched on exact tool name against a static list of SUNI's own
consequential tools. MCP tools are registered as "{server}_{tool}", so they
could never match — is_consequential() returned False for every one of them.
An MCP server that runs shell commands, deletes files or moves money was
therefore executed without approval, constrained only by the RBAC prefix list,
which for an admin is "all".

Classification is by the action words in the tool name, because MCP exposes no
"this mutates something" flag. The default for an unrecognised action is
CONSEQUENTIAL: a false prompt costs one click and can be dismissed permanently
with "always allow this"; a false pass runs the command.
"""
from __future__ import annotations

import pytest

from suni.approval import is_consequential, mcp_is_consequential
from suni.tools.registry import ToolRegistry


@pytest.fixture
def registry():
    r = ToolRegistry()
    for prefix in ("desktopcommander", "github", "playwright", "stripe",
                   "filesystem", "searxng", "markitdown", "dbhub", "netdata"):
        r.register_mcp_prefix(prefix)
    return r


# (tool, must_require_approval, why)
CASES = [
    # The reason this fix exists: an MCP server with a shell.
    ("desktopcommander_execute_command", True,  "executes arbitrary commands"),
    ("desktopcommander_kill_process",    True,  "kills processes"),
    ("dbhub_execute_sql",                True,  "runs arbitrary SQL"),
    ("stripe_create_payment_link",       True,  "moves money"),
    ("github_create_issue",              True,  "writes to a remote"),
    ("filesystem_write_file",            True,  "writes to disk"),
    ("playwright_browser_click",         True,  "can submit forms"),
    # Reads must NOT prompt, or the gate gets switched off.
    ("github_search_issues",             False, "read-only search"),
    ("github_get_file_contents",         False, "read-only fetch"),
    ("stripe_list_customers",            False, "read-only list"),
    ("filesystem_read_file",             False, "read-only"),
    ("searxng_search",                   False, "read-only search"),
    ("playwright_browser_navigate",      False, "navigation mutates nothing remote"),
    ("netdata_get_metrics",              False, "read-only metrics"),
]


@pytest.mark.parametrize("tool,expected,why", CASES,
                         ids=[f"{t}" for t, _, _ in CASES])
def test_mcp_tools_are_classified(registry, tool, expected, why):
    assert is_consequential(tool, registry=registry) is expected, why


def test_unknown_action_defaults_to_requiring_approval(registry):
    """An action nobody anticipated must not sail through."""
    assert is_consequential("github_frobnicate_widget", registry=registry) is True


def test_write_word_beats_read_word(registry):
    """create_or_update_file contains no read verb, but a tool like
    'get_or_create' must still be treated as a write."""
    assert mcp_is_consequential("get_or_create_repo") is True
    assert mcp_is_consequential("create_or_update_file") is True


def test_native_tools_still_gate_on_the_static_list():
    """The original behaviour is unchanged for SUNI's own tools."""
    assert is_consequential("run_shell") is True
    assert is_consequential("send_email") is True
    assert is_consequential("web_search") is False


def test_native_tools_are_not_misclassified_as_mcp(registry):
    """A SUNI tool whose name starts with a server prefix must not be
    reclassified — exact-list membership wins."""
    assert is_consequential("run_shell", registry=registry) is True


def test_without_a_registry_mcp_tools_are_not_classified():
    """Documents the failure mode: callers that do not pass the registry get
    the old, unsafe answer. This is why the orchestrator passes it."""
    assert is_consequential("desktopcommander_execute_command") is False


def test_orchestrator_passes_the_registry():
    """The one call site that matters must not regress to the unsafe form."""
    import inspect
    from suni.core import orchestrator
    src = inspect.getsource(orchestrator)
    call = src[src.index("is_consequential("):]
    call = call[:call.index(")")]
    assert "registry" in call, \
        "the orchestrator must pass registry= or every MCP tool skips approval"
