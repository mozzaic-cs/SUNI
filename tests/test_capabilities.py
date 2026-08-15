"""
SUNI offline capability tests using ScriptedAgent.
No Ollama or external services required.

Run:
  pytest tests/test_capabilities.py            # fast, no Ollama needed
  pytest tests/test_capabilities.py -v         # verbose output
"""
from __future__ import annotations
import asyncio
import numpy as np
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from suni.core.base_agent import BaseAgent
from suni.core.message import Message, Role, ToolCall
from suni.core.context import Context
from suni.core.orchestrator import Orchestrator
from suni.tools.registry import ToolRegistry
from suni.memory.manager import MemoryManager
from suni.memory.store import MemoryStore, _encode
from suni.memory.consolidator import _parse_extractions, dedup, extract_facts
import suni.approval as _approval
import suni.projects as _projects


# ─── ScriptedAgent ────────────────────────────────────────────────────────────

class ScriptedAgent(BaseAgent):
    """Returns pre-programmed Messages in order. No LLM required."""

    def __init__(self, responses: list[Message]):
        super().__init__(name="scripted")
        self._queue = list(responses)

    async def chat(self, messages, context, tools=None) -> Message:
        if not self._queue:
            return Message(role=Role.ASSISTANT, content="[script exhausted]", agent="scripted")
        return self._queue.pop(0)


# ─── Shared fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_approval_state():
    """Clear approval module globals before and after every test."""
    _approval._pending.clear()
    _approval._trust_rules.clear()
    yield
    for entry in list(_approval._pending.values()):
        if not entry["future"].done():
            entry["future"].cancel()
    _approval._pending.clear()
    _approval._trust_rules.clear()


@pytest.fixture(autouse=True)
def mock_embed_nomic(monkeypatch):
    """Return a deterministic 768-dim unit vector instead of calling Ollama.
    Keeps test_capabilities.py Ollama-free: all stored entries share the same
    vector, so cosine similarity is 1.0 and every search returns all results."""
    import suni.memory.manager as _mgr
    _UNIT: list[float] = [1.0] + [0.0] * 767

    # Accept whatever the real embed_nomic accepts. It gained host/model
    # parameters when the embedding backend became configurable, and this mock
    # was not updated — so every test touching memory failed with
    # "_fake() got an unexpected keyword argument 'host'". **kwargs keeps the
    # mock from going stale the next time a parameter is added.
    async def _fake(text: str, *args, **kwargs) -> list[float]:
        return _UNIT

    monkeypatch.setattr(_mgr, "embed_nomic", _fake)
    # The manager can also be pointed at an OpenAI-compatible endpoint; stub
    # that too so a configured backend cannot drag a live call into the suite.
    monkeypatch.setattr(_mgr, "embed_openai", _fake)


def _make_orch(
    responses: list[Message],
    registry: ToolRegistry | None = None,
) -> Orchestrator:
    return Orchestrator(primary=ScriptedAgent(responses), registry=registry or ToolRegistry())


# ─── Tool call roundtrip ──────────────────────────────────────────────────────

class TestScriptedToolRoundtrip:
    async def test_tool_start_and_end_events_fire(self):
        """Scripted tool call emits tool_start and tool_end events."""
        reg = ToolRegistry()
        invoked = []

        async def _greet(name: str) -> str:
            invoked.append(name)
            return f"hi {name}"

        reg.register(
            {
                "name": "greet",
                "description": "greet a person",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            },
            _greet,
        )
        script = [
            Message(
                role=Role.ASSISTANT, content="", agent="scripted",
                tool_calls=[ToolCall(id="t1", name="greet", args={"name": "Alice"})],
            ),
            Message(role=Role.ASSISTANT, content="Done.", agent="scripted"),
        ]
        orch = _make_orch(script, reg)
        events: list[dict] = []
        result = await orch.run("say hello", Context(), event_cb=events.append)

        event_types = {e["type"] for e in events}
        assert "tool_start" in event_types
        assert "tool_end" in event_types
        assert invoked == ["Alice"]
        assert result == "Done."

    async def test_tool_result_fed_back_as_tool_message(self):
        """Tool output appears as a TOOL-role message on the second agent call."""
        reg = ToolRegistry()

        async def _echo(val: str) -> str:
            return f"echoed:{val}"

        reg.register(
            {
                "name": "echo",
                "description": "echo a value",
                "parameters": {
                    "type": "object",
                    "properties": {"val": {"type": "string"}},
                    "required": ["val"],
                },
            },
            _echo,
        )

        second_call_messages: list = []

        class _Capturing(BaseAgent):
            def __init__(self):
                super().__init__(name="cap")
                self._n = 0

            async def chat(self, messages, context, tools=None) -> Message:
                self._n += 1
                if self._n == 2:
                    second_call_messages.extend(messages)
                    return Message(role=Role.ASSISTANT, content="saw the result", agent="cap")
                return Message(
                    role=Role.ASSISTANT, content="", agent="cap",
                    tool_calls=[ToolCall(id="t1", name="echo", args={"val": "secret42"})],
                )

        orch = Orchestrator(primary=_Capturing(), registry=reg)
        await orch.run("echo secret42", Context())

        tool_msgs = [m for m in second_call_messages if m.role == Role.TOOL]
        assert any("echoed:secret42" in m.content for m in tool_msgs)

    async def test_non_tool_response_exits_loop(self):
        """An agent response with no tool calls returns immediately."""
        script = [Message(role=Role.ASSISTANT, content="Simple answer.", agent="scripted")]
        orch = _make_orch(script)
        result = await orch.run("Tell me something", Context())
        assert result == "Simple answer."


# ─── Approval gate ────────────────────────────────────────────────────────────

class TestApprovalGate:
    async def test_allow_executes_consequential_tool(self):
        """Approving a consequential tool call runs the handler."""
        reg = ToolRegistry()
        executed: list[str] = []

        async def _run_shell(command: str) -> str:
            executed.append(command)
            return "ran"

        reg.register(
            {
                "name": "run_shell",
                "description": "run a shell command",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
            _run_shell,
        )
        script = [
            Message(
                role=Role.ASSISTANT, content="", agent="scripted",
                tool_calls=[ToolCall(id="t1", name="run_shell", args={"command": "echo hello"})],
            ),
            Message(role=Role.ASSISTANT, content="Command ran.", agent="scripted"),
        ]
        orch = _make_orch(script, reg)
        events: list[dict] = []

        async def _resolve_allow():
            await asyncio.sleep(0.05)
            aid = next(iter(_approval._pending), None)
            if aid:
                _approval.resolve_approval(aid, "allow", user_id="u1")

        asyncio.ensure_future(_resolve_allow())
        result = await orch.run("run shell command echo hello", Context(), user_id="u1", event_cb=events.append)

        assert any(e["type"] == "approval_request" for e in events)
        assert executed == ["echo hello"]
        assert result == "Command ran."

    async def test_deny_skips_consequential_tool(self):
        """Denying a consequential tool call prevents the handler from running."""
        reg = ToolRegistry()
        executed: list[str] = []

        async def _run_shell(command: str) -> str:
            executed.append(command)
            return "ran"

        reg.register(
            {
                "name": "run_shell",
                "description": "run a shell command",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
            _run_shell,
        )
        script = [
            Message(
                role=Role.ASSISTANT, content="", agent="scripted",
                tool_calls=[ToolCall(id="t1", name="run_shell", args={"command": "rm -rf /tmp/x"})],
            ),
            Message(role=Role.ASSISTANT, content="Action was denied.", agent="scripted"),
        ]
        orch = _make_orch(script, reg)

        async def _resolve_deny():
            await asyncio.sleep(0.05)
            aid = next(iter(_approval._pending), None)
            if aid:
                _approval.resolve_approval(aid, "deny", user_id="u1")

        asyncio.ensure_future(_resolve_deny())
        await orch.run("run shell command", Context(), user_id="u1")

        assert executed == []

    async def test_trust_rule_bypasses_approval_gate(self):
        """An always-allow trust rule prevents the approval_request event."""
        reg = ToolRegistry()
        executed: list[str] = []

        async def _run_shell(command: str) -> str:
            executed.append(command)
            return "ran"

        reg.register(
            {
                "name": "run_shell",
                "description": "run a shell command",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
            _run_shell,
        )
        _approval.add_trust_rule("u1", "run_shell", "*")

        script = [
            Message(
                role=Role.ASSISTANT, content="", agent="scripted",
                tool_calls=[ToolCall(id="t1", name="run_shell", args={"command": "ls"})],
            ),
            Message(role=Role.ASSISTANT, content="Done.", agent="scripted"),
        ]
        orch = _make_orch(script, reg)
        events: list[dict] = []
        await orch.run("list directory contents", Context(), user_id="u1", event_cb=events.append)

        assert not any(e["type"] == "approval_request" for e in events)
        assert executed == ["ls"]


# ─── RBAC enforcement ─────────────────────────────────────────────────────────

class TestRBACEnforcement:
    async def test_allowlist_filters_disallowed_tools(self):
        """
        read-only role uses an allowlist (not a blocklist).
        run_shell is not in the allowlist → absent from offered tools.
        web_search is in the allowlist → present in offered tools.
        """
        reg = ToolRegistry()

        async def _shell(command: str) -> str:
            return "ran"

        async def _search(query: str) -> str:
            return "results"

        reg.register(
            {"name": "run_shell", "description": "shell", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            _shell,
        )
        reg.register(
            {"name": "web_search", "description": "search", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
            _search,
        )

        seen_tools: list = []

        class _Spy(BaseAgent):
            def __init__(self):
                super().__init__(name="spy")

            async def chat(self, messages, context, tools=None) -> Message:
                if tools:
                    seen_tools.extend(tools)
                return Message(role=Role.ASSISTANT, content="ok", agent="spy")

        orch = Orchestrator(primary=_Spy(), registry=reg)
        await orch.run("test", Context(), user_role="read-only")

        offered_names = {t["function"]["name"] for t in seen_tools}
        # run_shell not in read-only allowlist — must be absent
        assert "run_shell" not in offered_names, (
            "run_shell should be filtered by read-only allowlist"
        )
        # web_search is in the read-only allowlist — must be present
        assert "web_search" in offered_names, (
            "web_search should be offered to read-only agents"
        )


# ─── Memory system ────────────────────────────────────────────────────────────

class TestMemorySystem:
    async def test_fact_stored_and_searchable(self, tmp_path):
        """A fact-bearing exchange is stored and returned by similarity search."""
        mm = MemoryManager(store_path=str(tmp_path / "mem.json"))
        await mm.add_exchange("My name is Alice", "Nice to meet you, Alice!")

        results = await mm.search("What is the user's name?")
        contents = " ".join(r["content"] for r in results)
        assert "Alice" in contents

    async def test_fact_type_detection(self, tmp_path):
        """add_exchange with a fact-like message creates a 'fact' typed entry."""
        mm = MemoryManager(store_path=str(tmp_path / "mem.json"))
        await mm.add_exchange("My name is Bob", "Hello, Bob!")

        facts = mm.store.get_by_type("fact")
        assert any("Bob" in f["content"] for f in facts)

    async def test_build_context_injects_relevant_memory(self, tmp_path):
        """build_context returns a formatted string containing relevant content."""
        mm = MemoryManager(store_path=str(tmp_path / "mem.json"))
        await mm.add_exchange("I work at Acme as a developer", "Got it!")

        ctx = await mm.build_context("Where do I work?")
        assert "Acme" in ctx
        assert "Relevant memories" in ctx

    async def test_memory_persists_after_reload(self, tmp_path):
        """MemoryStore data survives a reload from disk."""
        path = str(tmp_path / "persist.json")
        mm1 = MemoryManager(store_path=path)
        await mm1.add_exchange("My favorite color is indigo", "Noted!")

        mm2 = MemoryManager(store_path=path)
        results = await mm2.search("What is the user's favorite color?")
        contents = " ".join(r["content"] for r in results)
        assert "indigo" in contents


# ─── Projects CRUD ────────────────────────────────────────────────────────────

class TestProjectsCRUD:
    def test_create_and_get(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_projects, "_DB", tmp_path / "projects.db")

        proj = _projects.create_project("Alpha", goal="Achieve orbit", user_id="u1")
        fetched = _projects.get_project(proj["id"], "u1")

        assert fetched is not None
        assert fetched["name"] == "Alpha"
        assert fetched["goal"] == "Achieve orbit"
        assert fetched["status"] == "active"

    def test_list_scoped_per_user(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_projects, "_DB", tmp_path / "projects.db")

        _projects.create_project("P1", user_id="user_a")
        _projects.create_project("P2", user_id="user_a")
        _projects.create_project("P3", user_id="user_b")

        a_list = _projects.list_projects("user_a")
        b_list = _projects.list_projects("user_b")

        assert len(a_list) == 2
        assert len(b_list) == 1

    def test_member_access_control(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_projects, "_DB", tmp_path / "projects.db")

        proj = _projects.create_project("Collab", user_id="owner1")
        pid = proj["id"]
        _projects.add_member(pid, "viewer2", "viewer")

        assert _projects.can_access(pid, "owner1") is not None
        assert _projects.can_access(pid, "viewer2") is not None
        assert _projects.can_access(pid, "stranger") is None

    def test_update_project_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_projects, "_DB", tmp_path / "projects.db")

        proj = _projects.create_project("Old Name", user_id="u1")
        _projects.update_project(proj["id"], name="New Name", status="paused")
        updated = _projects.get_project(proj["id"])

        assert updated["name"] == "New Name"
        assert updated["status"] == "paused"

    def test_remove_last_owner_blocked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_projects, "_DB", tmp_path / "projects.db")

        proj = _projects.create_project("Solo", user_id="owner1")
        result = _projects.remove_member(proj["id"], "owner1")

        assert result is False
        members = _projects.get_members(proj["id"])
        assert any(m["user_id"] == "owner1" for m in members)


# ─── _user_id injection ──────────────────────────────────────────────────────

class TestUserIdInjection:
    """Regression tests for user_id threading from orchestrator → tool handler."""

    async def test_project_create_via_orch_is_visible_to_list(self, tmp_path, monkeypatch):
        """
        A project created by project_create through the orchestrator must be
        visible via list_projects for the requesting user.

        Previously: create_handler received _user_id="" → no project_members row
        → list_projects (INNER JOIN) returned nothing.
        """
        monkeypatch.setattr(_projects, "_DB", tmp_path / "projects.db")
        from suni.tools import project_tool

        reg = ToolRegistry()
        reg.register(project_tool.CREATE_SCHEMA, project_tool.create_handler)
        reg.register(project_tool.LIST_SCHEMA,   project_tool.list_handler)

        script = [
            Message(
                role=Role.ASSISTANT, content="", agent="scripted",
                tool_calls=[ToolCall(id="t1", name="project_create", args={"name": "InjectionTest", "goal": "verify uid"})],
            ),
            Message(role=Role.ASSISTANT, content="Project created.", agent="scripted"),
        ]
        orch = _make_orch(script, reg)
        await orch.run("create a project called InjectionTest", Context(), user_id="u_inject")

        visible = _projects.list_projects("u_inject")
        assert any(p["name"] == "InjectionTest" for p in visible), (
            f"Project not visible to creating user. list_projects returned: {visible}"
        )

    async def test_project_list_scoped_to_requesting_user(self, tmp_path, monkeypatch):
        """
        project_list must only return the requesting user's projects, not all users'.

        Previously: list_handler passed no user_id → list_projects returned every
        user's projects (cross-user data leak).
        """
        monkeypatch.setattr(_projects, "_DB", tmp_path / "projects.db")
        from suni.tools import project_tool

        _projects.create_project("UserA-Project", user_id="user_a")
        _projects.create_project("UserB-Project", user_id="user_b")

        reg = ToolRegistry()
        reg.register(project_tool.LIST_SCHEMA, project_tool.list_handler)

        captured_result: list[str] = []

        script = [
            Message(
                role=Role.ASSISTANT, content="", agent="scripted",
                tool_calls=[ToolCall(id="t1", name="project_list", args={})],
            ),
            Message(role=Role.ASSISTANT, content="Listed.", agent="scripted"),
        ]

        class _Capturing(BaseAgent):
            def __init__(self):
                super().__init__(name="cap")
                self._n = 0

            async def chat(self, messages, context, tools=None) -> Message:
                self._n += 1
                if self._n == 1:
                    return Message(
                        role=Role.ASSISTANT, content="", agent="cap",
                        tool_calls=[ToolCall(id="t1", name="project_list", args={})],
                    )
                tool_msgs = [m for m in messages if m.role == Role.TOOL]
                if tool_msgs:
                    captured_result.append(tool_msgs[-1].content)
                return Message(role=Role.ASSISTANT, content="Listed.", agent="cap")

        orch = Orchestrator(primary=_Capturing(), registry=reg)
        await orch.run("list my projects", Context(), user_id="user_a")

        assert captured_result, "No tool result captured"
        result_text = captured_result[0]
        assert "UserA-Project" in result_text, f"Own project missing from list: {result_text!r}"
        assert "UserB-Project" not in result_text, f"Other user's project leaked: {result_text!r}"


# ─── Memory consolidation ─────────────────────────────────────────────────────

class TestConsolidation:
    def test_parse_extractions_fact_and_preference(self):
        """_parse_extractions correctly parses [FACT] and [PREFERENCE] lines."""
        text = (
            "[FACT] I work at Acme\n"
            "[PREFERENCE] I prefer dark mode\n"
            "Irrelevant prose line\n"
            "[FACT] My name is Alex\n"
        )
        results = _parse_extractions(text)

        assert ("I work at Acme", "fact") in results
        assert ("I prefer dark mode", "preference") in results
        assert ("My name is Alex", "fact") in results
        assert len(results) == 3

    def test_parse_extractions_empty_input(self):
        assert _parse_extractions("") == []
        assert _parse_extractions("just prose, no tags") == []

    def test_dedup_removes_older_near_duplicate(self, tmp_path):
        """dedup() removes the older of two entries with cosine similarity >= threshold."""
        store = MemoryStore(str(tmp_path / "dedup.json"))

        vec = np.random.rand(384).astype(np.float32)
        vec /= np.linalg.norm(vec)

        store._data = [
            {
                "id": "old_entry",
                "content": "I work at Acme",
                "type": "fact",
                "timestamp": "2024-01-01T10:00:00",
                "e16": _encode(vec),
                "metadata": {},
            },
            {
                "id": "new_entry",
                "content": "My employer is Acme",
                "type": "fact",
                "timestamp": "2024-01-01T11:00:00",
                "e16": _encode(vec),
                "metadata": {},
            },
        ]

        removed = dedup(store)

        assert removed == 1
        # dedup supersedes rather than deletes (memory_supersede, default on):
        # the loser is marked deprecated and kept for auditability, so assert on
        # what is still ACTIVE. The test previously expected a hard delete and
        # failed once superseding landed.
        entries = store.get_all()
        active = [e for e in entries
                  if (e.get("metadata") or {}).get("lifecycle") != "deprecated"]
        assert len(active) == 1
        assert active[0]["id"] == "new_entry"

        superseded = [e for e in entries
                      if (e.get("metadata") or {}).get("lifecycle") == "deprecated"]
        if superseded:                       # only when superseding is enabled
            assert superseded[0]["id"] == "old_entry"
            assert superseded[0]["metadata"].get("superseded_by") == "new_entry"

    def test_dedup_leaves_dissimilar_entries(self, tmp_path):
        """dedup() does not remove entries with very different embeddings."""
        store = MemoryStore(str(tmp_path / "dedup2.json"))

        rng = np.random.default_rng(42)
        v1 = rng.standard_normal(384).astype(np.float32)
        v2 = rng.standard_normal(384).astype(np.float32)
        v1 /= np.linalg.norm(v1)
        v2 /= np.linalg.norm(v2)
        # Make them orthogonal (cosine = 0)
        v2 -= np.dot(v1, v2) * v1
        v2 /= np.linalg.norm(v2)

        store._data = [
            {"id": "e1", "content": "I like hiking", "type": "fact",
             "timestamp": "2024-01-01T10:00:00", "e16": _encode(v1), "metadata": {}},
            {"id": "e2", "content": "I love swimming", "type": "fact",
             "timestamp": "2024-01-01T11:00:00", "e16": _encode(v2), "metadata": {}},
        ]

        removed = dedup(store)

        assert removed == 0
        assert len(store.get_all()) == 2

    def test_dedup_ignores_conversation_entries(self, tmp_path):
        """dedup() never removes conversation-type entries, only fact/preference."""
        store = MemoryStore(str(tmp_path / "dedup3.json"))

        vec = np.random.rand(384).astype(np.float32)
        vec /= np.linalg.norm(vec)

        store._data = [
            {"id": "c1", "content": "User asked about Acme", "type": "conversation",
             "timestamp": "2024-01-01T10:00:00", "e16": _encode(vec), "metadata": {}},
            {"id": "c2", "content": "User asked about Acme again", "type": "conversation",
             "timestamp": "2024-01-01T11:00:00", "e16": _encode(vec), "metadata": {}},
        ]

        removed = dedup(store)

        assert removed == 0
        assert len(store.get_all()) == 2

    async def test_extract_facts_with_mock_llm(self, tmp_path):
        """extract_facts() with a mocked LLM stores parsed fact/preference entries."""
        mm = MemoryManager(store_path=str(tmp_path / "mem.json"))
        await mm.add_exchange("I love hiking on weekends", "That sounds wonderful!")

        mock_output = (
            "[FACT] The user loves hiking on weekends\n"
            "[PREFERENCE] The user prefers outdoor activities\n"
        )

        with patch("suni.memory.consolidator._call_ollama", new=AsyncMock(return_value=mock_output)):
            count = await extract_facts(mm)

        assert count == 2

        all_entries = mm.store.get_all()
        extracted = [e for e in all_entries if e.get("metadata", {}).get("source") == "llm-extracted"]
        assert len(extracted) == 2

        contents = " ".join(e["content"] for e in extracted)
        assert "hiking" in contents
        assert "outdoor" in contents

    async def test_extract_facts_respects_since_ts(self, tmp_path):
        """extract_facts() with since_ts skips conversations older than the cutoff."""
        mm = MemoryManager(store_path=str(tmp_path / "mem.json"))
        await mm.add_exchange("Old fact from last week", "Ok")

        # Set since_ts to future — no conversations qualify
        future_ts = "2099-01-01T00:00:00"

        with patch("suni.memory.consolidator._call_ollama", new=AsyncMock(return_value="[FACT] something")) as m:
            count = await extract_facts(mm, since_ts=future_ts)

        assert count == 0
        m.assert_not_called()
