"""
Agent profiles: storage, and the rule that they may only ever NARROW access.

The escalation this guards against: an agent profile declares the tools and MCP
servers it uses. If that declaration were applied as given, a restricted user
picking an admin-authored agent out of a dropdown would inherit admin tool
reach — privilege escalation through a config file, with no prompt and no audit
signal that anything unusual happened.

So grants INTERSECT with the caller's role and blocks UNION with it. The
resolution happens at invocation time rather than being stored, because
AGENT.md is a file on disk that a user can edit outside the application.

Same class of hole as tests/test_mcp_approval.py: a new surface that reaches the
tool layer without going through the checks the old surface went through.
"""
from __future__ import annotations

import os

import pytest

from suni import agents


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Never touch the real memory/ directory."""
    monkeypatch.setattr(agents, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(agents, "AGENTS_DB", tmp_path / "agents.db")
    yield


@pytest.fixture
def fake_rbac(monkeypatch):
    """A restricted role: two tools, one MCP server, assistant mode only."""
    monkeypatch.setattr(agents._rbac, "allowed_tools", lambda role:
                        None if role == "admin" else ["web_search", "read_file"])
    monkeypatch.setattr(agents._rbac, "blocked_tools", lambda role:
                        [] if role == "admin" else ["run_shell"])
    monkeypatch.setattr(agents._rbac, "mcp_prefixes", lambda role, reg:
                        reg if role == "admin" else ["filesystem"])
    monkeypatch.setattr(agents._rbac, "allowed_modes", lambda role:
                        ["assistant", "task", "read-only", "collaborate"]
                        if role == "admin" else ["assistant"])


REGISTERED = ["filesystem", "playwright", "github"]


# ── the escalation cases ─────────────────────────────────────────────────────
def test_agent_cannot_widen_a_restricted_user(fake_rbac):
    """An admin-authored agent asking for run_shell must not grant it."""
    agent = {"tools": ["web_search", "run_shell", "send_email"], "mode": "assistant"}
    g = agents.effective_grants(agent, "user", REGISTERED)
    assert g["allowed_tools"] == ["web_search"], \
        "the agent widened a restricted user's tool set"
    assert "run_shell" not in (g["allowed_tools"] or [])


def test_agent_cannot_widen_mcp_reach(fake_rbac):
    agent = {"mcp_servers": ["filesystem", "playwright", "github"]}
    g = agents.effective_grants(agent, "user", REGISTERED)
    assert g["mcp_prefixes"] == ["filesystem"], "the agent widened MCP reach"


def test_agent_cannot_unblock_what_the_role_blocks(fake_rbac):
    """Blocks union — an agent may add restrictions, never remove them."""
    agent = {"tools": ["web_search"], "blocked": ["web_fetch"]}
    g = agents.effective_grants(agent, "user", REGISTERED)
    assert "run_shell" in g["blocked_tools"], "the agent removed a role-level block"
    assert "web_fetch" in g["blocked_tools"], "the agent's own block was dropped"


def test_agent_cannot_grant_a_mode_the_role_lacks(fake_rbac):
    """'task' can execute plans; a role limited to assistant must stay there."""
    g = agents.effective_grants({"mode": "task"}, "user", REGISTERED)
    assert g["mode"] == "assistant"


# ── narrowing, which is the point of the feature ─────────────────────────────
def test_agent_narrows_an_admin(fake_rbac):
    """Admin has no tool restriction; the agent supplies one."""
    agent = {"tools": ["web_search"], "mcp_servers": ["filesystem"]}
    g = agents.effective_grants(agent, "admin", REGISTERED)
    assert g["allowed_tools"] == ["web_search"]
    assert g["mcp_prefixes"] == ["filesystem"]


def test_none_means_inherit_not_everything(fake_rbac):
    """An agent that declares no tools leaves the caller's grants untouched."""
    g = agents.effective_grants({"tools": None, "mcp_servers": None}, "user", REGISTERED)
    assert g["allowed_tools"] == ["web_search", "read_file"]
    assert g["mcp_prefixes"] == ["filesystem"]


def test_unrestricted_stays_unrestricted_as_none(fake_rbac):
    """None must be preserved, not expanded into a snapshot of today's tools —
    freezing the list would silently drop tools registered later."""
    g = agents.effective_grants({"tools": None}, "admin", REGISTERED)
    assert g["allowed_tools"] is None


def test_no_agent_is_the_plain_role(fake_rbac):
    g = agents.effective_grants(None, "user", REGISTERED)
    assert g["allowed_tools"] == ["web_search", "read_file"]
    assert g["system_prompt"] == "" and g["model"] == ""


# ── storage round-trip ───────────────────────────────────────────────────────
def test_create_and_get_round_trip():
    rec = agents.create(
        name="Research Assistant",
        system_prompt="You research topics and cite sources.",
        owner_id="u1",
        description="reads, does not write",
        model="qwen2.5:7b",
        tools=["web_search", "web_fetch"],
    )
    assert rec["slug"] == "research-assistant"
    got = agents.get("research-assistant")
    assert got is not None
    assert got["name"] == "Research Assistant"
    assert got["model"] == "qwen2.5:7b"
    assert got["tools"] == ["web_search", "web_fetch"]
    assert "cite sources" in got["system_prompt"]


def test_prompt_is_read_from_the_file_so_it_can_be_edited():
    """AGENT.md is the source of truth for the prompt — editing it outside the
    app must take effect, which is the reason for the file format."""
    agents.create(name="Editable", system_prompt="original", owner_id="u1")
    md = agents.AGENTS_DIR / "editable" / "AGENT.md"
    md.write_text(md.read_text(encoding="utf-8").replace("original", "edited by hand"),
                  encoding="utf-8")
    assert agents.get("editable")["system_prompt"] == "edited by hand"


def test_edited_file_still_cannot_widen_grants(fake_rbac):
    """The corollary: the file is editable, so its grants are never trusted."""
    agents.create(name="Sneaky", system_prompt="x", owner_id="u1", tools=["web_search"])
    rec = agents.get("sneaky")
    rec["tools"] = ["run_shell", "send_email"]        # as if edited on disk
    g = agents.effective_grants(rec, "user", REGISTERED)
    assert g["allowed_tools"] == [], "hand-edited grants were trusted"


def test_permissions_on_delete():
    agents.create(name="Mine", system_prompt="x", owner_id="u1")
    assert agents.delete("mine", "u2") is False, "a stranger deleted someone's agent"
    assert agents.delete("mine", "u1") is True
    assert agents.get("mine") is None


def test_admin_sees_and_edits_everything():
    agents.create(name="Theirs", system_prompt="x", owner_id="u1")
    assert agents.can_edit("theirs", "u2", "admin") is True
    assert len(agents.list_for_user("u2", "admin")) == 1
    assert len(agents.list_for_user("u2", "user")) == 0
