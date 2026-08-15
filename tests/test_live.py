"""
SUNI live capability tests — require Ollama running at localhost:11434.

Run:
  pytest tests/test_live.py -m live              # all live tests
  pytest tests/test_live.py -m live -v           # verbose
  pytest tests/test_live.py -m live -s           # show print output

These tests exercise SUNI's features through the full orchestrator pipeline
with a real LLM. They are intentionally excluded from fast/CI runs via
the `live` marker.
"""
from __future__ import annotations
import asyncio
import pytest
from pathlib import Path

from suni.core.context import Context
from suni.core.message import Message, Role
from suni.core.orchestrator import Orchestrator
from suni.models.ollama_agent import OllamaAgent
from suni.memory.manager import MemoryManager
from suni.tools.registry import ToolRegistry
from suni.tools import project_tool, contacts_tool
import suni.approval as _approval
import suni.projects as _projects
from suni.main import SUNI_SYSTEM


# ─── Skip guard ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def ollama_available():
    """Skip all live tests if Ollama is not reachable."""
    import httpx
    try:
        r = httpx.get("http://localhost:11434", timeout=3.0)
        return r.status_code < 500
    except Exception:
        return False


@pytest.fixture(autouse=True)
def require_ollama(request, ollama_available):
    if request.node.get_closest_marker("live") and not ollama_available:
        pytest.skip("Ollama not available at localhost:11434")


@pytest.fixture(autouse=True)
def reset_approval_state():
    _approval._pending.clear()
    _approval._trust_rules.clear()
    yield
    for entry in list(_approval._pending.values()):
        if not entry["future"].done():
            entry["future"].cancel()
    _approval._pending.clear()
    _approval._trust_rules.clear()


# ─── Orchestrator builder ─────────────────────────────────────────────────────

# Minimal prompt for tests where SUNI_SYSTEM's "check skills_list first" instruction
# would cause the model to hallucinate a skills response instead of answering.
# Includes explicit memory instruction as belt-and-suspenders — injected context is
# now forwarded to the model (via _to_ollama merge fix), but a directive helps small
# models reliably follow it.
_MINIMAL_SYSTEM = (
    "You are a helpful assistant. Answer questions directly and concisely. "
    "IMPORTANT: When memory context is provided (lines starting with [fact] or [preference]), "
    "you MUST use those facts when answering questions about the user. "
    "Do not say you lack information when the memory context contains the answer."
)


def _make_live_orch(
    memory: MemoryManager | None = None,
    model: str = "qwen2.5:7b",
    extra_tools: list | None = None,
    system_prompt: str = SUNI_SYSTEM,
) -> Orchestrator:
    """Build a real Orchestrator with Ollama agent and a minimal tool set."""
    reg = ToolRegistry()

    # Core project tools
    reg.register(project_tool.CREATE_SCHEMA, project_tool.create_handler)
    reg.register(project_tool.LIST_SCHEMA,   project_tool.list_handler)
    reg.register(project_tool.GET_SCHEMA,    project_tool.get_handler)
    reg.register(project_tool.LOG_SCHEMA,    project_tool.log_handler)
    reg.register(project_tool.UPDATE_SCHEMA, project_tool.update_handler)

    # Contacts tool (useful for breadth)
    reg.register(contacts_tool.SEARCH_SCHEMA, contacts_tool.search_handler)
    reg.register(contacts_tool.ADD_SCHEMA,    contacts_tool.add_handler)

    if extra_tools:
        for schema, handler in extra_tools:
            reg.register(schema, handler)

    agent = OllamaAgent(
        name="suni_live_test",
        model=model,
        system_prompt=system_prompt,
    )
    return Orchestrator(primary=agent, registry=reg, memory=memory)


# ─── Live capability tests ────────────────────────────────────────────────────

@pytest.mark.live
class TestLiveCapabilities:
    async def test_basic_response_is_non_empty(self):
        """Orchestrator returns a non-empty string for a simple question."""
        orch = _make_live_orch()
        result = await orch.run("What is 2 plus 2?", Context())
        assert isinstance(result, str)
        assert len(result.strip()) > 0

    async def test_memory_retention_across_turns(self, tmp_path):
        """
        A fact stated in turn 1 is stored and injected into context of turn 2,
        and the model's answer reflects the remembered fact.

        Uses a minimal system prompt to avoid SUNI_SYSTEM's "check skills_list
        first" instruction, which causes qwen2.5 to hallucinate a skills response
        when skills_list is not registered.
        """
        mm = MemoryManager(store_path=str(tmp_path / "live_mem.json"))
        orch = _make_live_orch(memory=mm, system_prompt=_MINIMAL_SYSTEM)
        ctx1 = Context()

        await orch.run(
            "My favourite programming language is COBOL.",
            ctx1,
            user_id="mem_test_user",
        )

        # Verify the fact was stored in the memory store
        assert mm.store.count() > 0, "Nothing was stored in memory after turn 1"

        # Fresh context — memory retrieval should inject the stored fact
        ctx2 = Context()
        result = await orch.run(
            "What is my favourite programming language?",
            ctx2,
            user_id="mem_test_user",
        )

        # Structural: memory system message was injected with the relevant fact
        mem_msgs = [m for m in ctx2.history if m.agent == "memory"]
        injected = " ".join(m.content for m in mem_msgs).lower()
        assert "cobol" in injected, (
            f"Expected 'COBOL' in injected memory context, got: {injected!r}"
        )

        # Model response must reference the remembered language.
        assert "cobol" in result.lower(), (
            f"Model did not use injected memory fact. Response: {result!r}"
        )

    async def test_project_creation_via_chat(self, tmp_path, monkeypatch):
        """
        Asking SUNI to create a project results in a row visible to the requesting
        user via list_projects (verifies _user_id injection is working end-to-end).
        """
        db_path = tmp_path / "live_projects.db"
        monkeypatch.setattr(_projects, "_DB", db_path)
        orch = _make_live_orch()
        events: list[dict] = []

        result = await orch.run(
            "Please create a project called LiveCapTest with the goal: capability testing.",
            Context(),
            user_id="live_test_user",
            event_cb=events.append,
        )

        # Verify the tool was called
        tool_starts = [e for e in events if e.get("type") == "tool_start"]
        called_tools = {e["name"] for e in tool_starts}
        assert "project_create" in called_tools, (
            f"project_create tool not called. Called: {called_tools}. Response: {result!r}"
        )

        # Verify the project is visible to the requesting user via list_projects
        visible = _projects.list_projects("live_test_user")
        assert any("LiveCapTest" in p["name"] for p in visible), (
            f"Project not visible to live_test_user via list_projects. "
            f"list returned: {[p['name'] for p in visible]}. Response: {result!r}"
        )

    async def test_language_override_portuguese(self):
        """
        response_language='pt-PT' injects the Portuguese language instruction and the
        LLM responds in Portuguese when given a Portuguese-language prompt.
        """
        orch = _make_live_orch()
        ctx = Context()

        result = await orch.run(
            "Em português: quanto é dois mais dois? Responde com uma frase curta.",
            ctx,
            response_language="pt-PT",
        )

        # Structural check: language system message was injected
        lang_msgs = [m for m in ctx.history if m.agent == "lang"]
        assert len(lang_msgs) >= 1, "Language instruction not injected into context"
        assert "Portugal" in lang_msgs[0].content, (
            f"Expected Portuguese instruction, got: {lang_msgs[0].content!r}"
        )

        # LLM response check: expect Portuguese vocabulary in the answer
        result_lower = result.lower()
        portuguese_markers = [
            "quatro", "dois", "igual", "soma", "resultado", "é quatro",
            "quatro.", "é 4", "=4", "= 4",
        ]
        has_portuguese = any(m in result_lower for m in portuguese_markers)
        assert has_portuguese, (
            f"Expected Portuguese response, got: {result!r}"
        )

    async def test_multi_turn_conversation_context(self, tmp_path):
        """
        A 3-turn conversation stays coherent — the third response references
        content introduced in turn 1.
        """
        mm = MemoryManager(store_path=str(tmp_path / "multi.json"))
        orch = _make_live_orch(memory=mm)
        ctx = Context()  # shared context = conversation history

        await orch.run(
            "I'm working on a project called Helios that involves solar panel optimization.",
            ctx,
            user_id="multi_user",
        )
        await orch.run(
            "What technologies are typically used in solar optimization?",
            ctx,
            user_id="multi_user",
        )
        result = await orch.run(
            "Given what I told you earlier about my project, what would be a good first step?",
            ctx,
            user_id="multi_user",
        )

        # The response should reflect awareness of the Helios project context
        assert len(result.strip()) > 50, "Response too short to be meaningful"
        assert any(kw in result.lower() for kw in ("helios", "solar", "panel", "project", "optimization")), (
            f"Response doesn't reference earlier context: {result!r}"
        )

    async def test_tool_event_structure(self):
        """
        tool_start events have the required keys: type, name, args, iteration.
        tool_end events have: type, name, result, elapsed_ms, ok.
        """
        orch = _make_live_orch()
        events: list[dict] = []

        await orch.run(
            "Please create a project called EventStructureTest with goal: verify event keys.",
            Context(),
            user_id="event_test_user",
            event_cb=events.append,
        )

        tool_starts = [e for e in events if e.get("type") == "tool_start"]
        tool_ends   = [e for e in events if e.get("type") == "tool_end"]

        # Only validate structure if tools were actually called
        if tool_starts:
            for ev in tool_starts:
                assert "name" in ev, f"tool_start missing 'name': {ev}"
                assert "args" in ev, f"tool_start missing 'args': {ev}"
                assert "iteration" in ev, f"tool_start missing 'iteration': {ev}"

        if tool_ends:
            for ev in tool_ends:
                assert "name" in ev, f"tool_end missing 'name': {ev}"
                assert "result" in ev, f"tool_end missing 'result': {ev}"
                assert "elapsed_ms" in ev, f"tool_end missing 'elapsed_ms': {ev}"
                assert "ok" in ev, f"tool_end missing 'ok': {ev}"
