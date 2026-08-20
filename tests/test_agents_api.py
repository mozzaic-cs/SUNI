"""
Agents must be reachable from the interfaces, and only the right ones.

The engine landed before any surface, so these cover the surface: that the chat
endpoint resolves a selected agent server-side, that a user cannot invoke one
they have no access to, that the profile is never taken from the request body,
and that the audit row records which agent answered.
"""
from __future__ import annotations

import inspect
import re

import pytest

from suni.web import server as srv


@pytest.fixture(scope="module")
def src() -> str:
    # server.py carries a BOM; getsource is fine but read the file the same way
    # the interpreter does to keep offsets honest.
    return inspect.getsource(srv)


def test_client_sends_a_name_never_a_profile(src):
    """Accepting a profile object from the body would let a caller forge grants.

    The intersection in effective_grants() would still clamp it to the role, so
    this is defence in depth rather than the only guard — but a forged profile
    could still supply an arbitrary system prompt, which is its own problem.
    """
    i = src.index("_agent_slug = str(body.get(")
    assert 'body.get("agent")' in src[i:i + 80], \
        "the agent is read from the body as something other than a name"
    assert 'body.get("agent_profile")' not in src, \
        "the endpoint accepts a caller-supplied profile object"


def test_selection_is_checked_against_what_the_user_may_use(src):
    i = src.index("_agent_slug = str(body.get(")
    block = src[i:i + 900]
    assert "list_for_user" in block, "no visibility check on the selected agent"
    assert "_visible" in block and "in _visible" in block


def test_disabled_agents_cannot_be_invoked(src):
    i = src.index("_agent_slug = str(body.get(")
    assert 'get("enabled"' in src[i:i + 900], "a disabled agent can still be run"


def test_profile_reaches_the_orchestrator(src):
    """Anchor on the WEB chat call specifically — _safe_run has several callers
    (Slack, Telegram, Discord), and the first one in the file is not this one."""
    i = src.index("memory_override=user_memory")
    call = src[i - 400:i + 400]
    assert "agent_profile=_agent_profile" in call, \
        "the resolved profile never reaches the orchestrator"


def test_channel_handlers_do_not_silently_gain_agents(src):
    """Telegram/Discord/Slack run as a fixed role with no agent selection. If
    that ever changes, the profile must be resolved for the channel user rather
    than inherited, so this records the current boundary."""
    for handler in ("_uid = f\"telegram_", "_uid = f\"discord_", "_uid = f\"slack_"):
        if handler in src:
            i = src.index(handler)
            assert "agent_profile" not in src[i:i + 600], \
                "a channel handler passes an agent profile without resolving it"


def test_audit_row_records_which_agent_answered(src):
    i = src.index('route="chat"')
    block = src[i - 400:i + 500]
    assert "agent_slug=" in block, \
        "the chat audit row does not record the agent — runs are untraceable"


def test_creating_and_deleting_an_agent_is_audited(src):
    assert 'log_event(user["id"], user["username"], "agent.created"' in src
    assert '"agent.deleted"' in src


def test_list_reports_effective_not_declared_grants(src):
    """A restricted user must not be shown reach their agent will not get."""
    i = src.index('@app.get("/api/agents")')
    block = src[i:src.index('@app.post("/api/agents")')]
    assert "effective_grants" in block, "the list shows the file's declared grants"
    assert '"effective"' in block


def test_any_authenticated_user_may_author(src):
    """Authoring is safe because a profile can only narrow its invoker."""
    i = src.index('@app.post("/api/agents")')
    block = src[i:i + 700]
    assert "Depends(get_current_user)" in block
    assert "role" not in block.split("async def")[1].split("body =")[0], \
        "creation appears gated on a role; any authenticated user should author"


def test_delete_enforces_ownership(src):
    i = src.index('@app.delete("/api/agents/{slug}")')
    block = src[i:i + 500]
    assert "_agents.delete(slug, user[\"id\"], role)" in block, \
        "delete does not pass the caller — ownership is not enforced"
