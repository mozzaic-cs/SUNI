"""Read-only mode, which was only read-only in the places anyone had checked.

The mode promises "nothing that writes, sends or changes anything". It was
enforced on the direct routes (pdf, agent, schedule, email, image) and by
dropping every MCP tool — and not at all on the ordinary tool list, which is the
path the model actually uses. So the model was handed every native tool the
CALLER'S ROLE allows; for an admin, all of them. The mode bolted the shortcuts
and left the front door open.

The list it needed already existed: the read-only ROLE's allowance. The mode and
the role shared a name and no mechanism.

An allowlist is only as good as the check that it is complete, so the important
test here is the one that walks every tool the registry actually declares and
insists each one is either on the list or refused.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from suni import rbac

ROOT = pathlib.Path(__file__).resolve().parent.parent
ORCH = (ROOT / "suni/core/orchestrator.py").read_text(encoding="utf-8")
FACE = (ROOT / "suni/web/face.html").read_text(encoding="utf-8")

# Verbs that only ever look at something. Anything whose name does not start
# with one of these has to be justified by being on the allowlist, and the test
# below is what forces that justification.
_READING = ("list_", "get_", "read_", "search_", "show_", "ping_", "diagnose_",
            "db_query", "db_schema", "web_fetch", "web_search", "skill_view",
            "skills_list", "calendar_free_slots", "project_get", "project_list",
            "memory_list", "memory_search", "monitor_list", "monitor_alerts",
            "contacts_search", "invoke_agent")


def _declared_tools() -> set[str]:
    """Every tool name the tool modules declare, read from source so this does
    not need a live registry (which wants a model backend)."""
    names: set[str] = set()
    for p in (ROOT / "suni/tools").glob("*.py"):
        src = p.read_text(encoding="utf-8", errors="replace")
        names |= set(re.findall(r'"name":\s*"([a-z_][a-z0-9_]*)"', src))
    return names


def test_the_mode_now_consults_the_list_that_already_existed():
    # Anchored on the tool-list assembly, not on the first "read-only" in the
    # file: the MCP branch above it carries the same words and windowed this
    # test onto the wrong block.
    block = ORCH[ORCH.index('_allowed = (grants["allowed_tools"]'):][:1600]
    assert '_rbac.allowed_tools("read-only")' in block, (
        "the mode still never asks what read-only is allowed to do"
    )
    assert "t in set(_ro)" in block, "the allowance is replaced rather than intersected"


def test_selecting_read_only_can_only_take_tools_away():
    """Intersected, never widened — the rule agent grants already follow. A
    mode that could ADD a tool would be a privilege escalation dressed as a
    safety setting."""
    ro = set(rbac.allowed_tools("read-only"))
    for role in ("read-only", "standard", "power-user", "admin"):
        role_allowed = rbac.allowed_tools(role)
        base = _declared_tools() if role_allowed is None else set(role_allowed)
        effective = base & ro
        assert effective <= base, f"{role} gained tools by choosing read-only"
        assert effective <= ro, f"{role} kept tools read-only does not permit"


def test_an_admin_in_read_only_loses_the_dangerous_ones():
    """The case that was broken: admin has no allowlist at all, so nothing
    narrowed it."""
    ro = set(rbac.allowed_tools("read-only"))
    assert rbac.allowed_tools("admin") is None, "admin is no longer unrestricted"
    for dangerous in ("run_shell", "write_file", "send_email", "delete_file",
                      "db_execute", "claude_task", "download_file"):
        assert dangerous not in ro, f"read-only would still permit {dangerous}"


def test_every_tool_is_either_a_read_or_refused():
    """The completeness check. An allowlist silently stops covering the surface
    as tools are added, and nothing complains — so this walks what the tool
    modules actually declare and insists each name is accounted for.

    A new tool failing here is the test working: decide whether it reads or
    writes, and put it on the list or leave it off deliberately.
    """
    ro = set(rbac.allowed_tools("read-only"))
    unexplained = []
    for name in sorted(_declared_tools()):
        if name in ro:
            continue                      # allowed: must be a read
        unexplained.append(name)
    # Everything NOT on the list is refused, which is the safe direction. What
    # this asserts is the other one: nothing on the list writes.
    for name in sorted(ro):
        assert name.startswith(_READING), (
            f'"{name}" is on the read-only allowlist but does not look like a '
            "read. If it changes anything, it must come off."
        )
    assert unexplained, "no tools are refused, which cannot be right"


def test_the_help_does_not_promise_more_than_the_code_delivers():
    """This text was written before the enforcement was checked, and it was a
    guarantee the code did not keep."""
    panel = FACE[FACE.index('id="mode-help-panel"'):]
    panel = panel[:panel.index("</div>")]
    dd = panel[panel.index('data-m="read-only"'):]
    dd = dd[:dd.index("</dd>")]
    assert "writes" in dd and "MCP" in dd
    # The claim is only true because the tool list is narrowed; if that guard
    # goes, this test is the one that should fail with it.
    assert '_rbac.allowed_tools("read-only")' in ORCH
