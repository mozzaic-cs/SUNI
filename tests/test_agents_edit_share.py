"""
Editing an agent, and sharing one.

Both were missing in a way worth naming. There was no update path at all, so
changing a prompt meant delete-and-recreate — throwing away usage history and
orphaning every audit row that pointed at the old slug. And agent_members was
written on create and honoured by can_edit(), but nothing could add a member:
the sharing model was real in the schema, enforced by the permission checks, and
unreachable. A control that reads as configuration and reaches nothing.
"""
from __future__ import annotations

import pytest

from suni import agents


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(agents, "AGENTS_DB", tmp_path / "agents.db")
    yield


@pytest.fixture
def a():
    return agents.create(name="Research", system_prompt="original prompt",
                         owner_id="owner", description="first", tools=["web_search"])


# ── editing ──────────────────────────────────────────────────────────────────
def test_update_changes_fields_and_keeps_the_slug(a):
    rec = agents.update("research", "owner", "", name="Research v2",
                        description="second", model="qwen2.5:7b")
    assert rec["slug"] == "research"          # schedules and audit rows point here
    assert rec["name"] == "Research v2" and rec["model"] == "qwen2.5:7b"


def test_update_preserves_usage_history(a):
    agents.mark_used("research")
    agents.mark_used("research")
    agents.update("research", "owner", "", description="edited")
    assert agents.get("research")["used_count"] == 2, \
        "editing reset the usage history — the reason delete-and-recreate was wrong"


def test_update_rewrites_the_file_too(a):
    agents.update("research", "owner", "", system_prompt="a new prompt")
    assert agents.get("research")["system_prompt"] == "a new prompt"
    assert "a new prompt" in (agents.AGENTS_DIR / "research" / "AGENT.md").read_text(
        encoding="utf-8")


def test_a_stranger_cannot_edit(a):
    assert agents.update("research", "someone-else", "", name="hijacked") is None
    assert agents.get("research")["name"] == "Research"


def test_owner_cannot_be_reassigned(a):
    agents.update("research", "owner", "", owner_id="attacker")
    assert agents.get("research")["owner_id"] == "owner"


def test_the_endpoint_refuses_to_forward_protected_fields():
    """slug and owner_id are what schedules and audit rows point at.

    update() takes slug positionally, so passing it as a field is a TypeError
    rather than a silent rename — but the real guard is the endpoint's
    allowlist, since that is what untrusted input reaches.
    """
    import inspect
    from suni.web import server as srv
    src = inspect.getsource(srv)
    i = src.index('@app.patch("/api/agents/{slug}")')
    block = src[i:i + 1200]
    allow = block[block.index("if k in ("):block.index("))", block.index("if k in ("))]
    assert "slug" not in allow and "owner_id" not in allow, \
        "the update endpoint forwards a field that identifies the agent"
    assert "system_prompt" in allow and "name" in allow


def test_disabling_through_update_sticks(a):
    agents.update("research", "owner", "", enabled=False)
    assert agents.get("research")["enabled"] is False


# ── sharing, which previously could not happen ───────────────────────────────
def test_sharing_makes_an_agent_visible_to_someone_else(a):
    assert agents.list_for_user("colleague") == []
    assert agents.share("research", "colleague", "viewer", "owner") is True
    assert [x["slug"] for x in agents.list_for_user("colleague")] == ["research"]


def test_a_viewer_cannot_edit_but_an_editor_can(a):
    agents.share("research", "v", "viewer", "owner")
    agents.share("research", "e", "editor", "owner")
    assert agents.can_edit("research", "v") is False
    assert agents.can_edit("research", "e") is True


def test_only_viewer_or_editor_roles_are_accepted(a):
    assert agents.share("research", "x", "owner", "owner") is False
    assert agents.share("research", "x", "admin", "owner") is False


def test_a_stranger_cannot_share_someone_elses_agent(a):
    assert agents.share("research", "x", "viewer", "not-the-owner") is False


def test_unshare_removes_access(a):
    agents.share("research", "colleague", "viewer", "owner")
    assert agents.unshare("research", "colleague", "owner") is True
    assert agents.list_for_user("colleague") == []


def test_the_owner_cannot_be_unshared(a):
    """Otherwise an editor could orphan someone else's agent."""
    assert agents.unshare("research", "owner", "owner") is False
    assert [x["slug"] for x in agents.list_for_user("owner")] == ["research"]


def test_members_lists_the_owner_and_the_shares(a):
    agents.share("research", "colleague", "editor", "owner")
    ids = {m["user_id"]: m["role"] for m in agents.members("research")}
    assert ids["owner"] == "owner" and ids["colleague"] == "editor"
