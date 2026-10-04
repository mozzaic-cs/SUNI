"""A real chat turn must reach code_task.

The tool working when called directly proved nothing about whether a chat
turn ever gets there. On the reference box force_claude_code is on, so every
turn went to the Claude Code CLI in the home folder, with no edit rights, and
code_task was unreachable. Collaborate mode and task-mode plans also return
before the usual routes. These turns go through the real Orchestrator.run,
with a model that fails the test if anything asks it.
"""
import asyncio

import pytest

from suni import approval
from suni.core.context import Context
from suni.core.orchestrator import Orchestrator
from suni.tools import code_project as cp
from suni.tools.registry import ToolRegistry


class _NoModel:
    name = "must-not-be-called"

    async def chat(self, *a, **k):
        raise AssertionError("a model was asked; the turn should have gone to code_task")


@pytest.fixture
def setup(monkeypatch, tmp_path):
    root = tmp_path / "Projects"
    (root / "shop").mkdir(parents=True)
    (root / "my shop").mkdir()
    real_get = cp._cfg.get
    values = {"code_project_roots": [str(root)], "force_claude_code": True,
              "collaborate_enabled": True}
    monkeypatch.setattr(cp._cfg, "get", lambda k, d=None: values[k] if k in values else real_get(k, d))

    calls = []

    async def fake_code_task(**args):
        calls.append(args)
        return "Claude Code finished.\n\nFixed it.\n\n[code session: s-1]"

    async def allow(tool, args, user_id, event_cb, risk=None):
        return "allow"
    monkeypatch.setattr(approval, "request_approval", allow)

    reg = ToolRegistry()
    reg.register(cp.SCHEMA, fake_code_task)
    orch = Orchestrator(_NoModel(), reg)
    return orch, root, calls


def _turn(orch, text, *, mode="assistant", role="admin", ctx=None):
    events = []
    out = asyncio.run(orch.run(text, ctx or Context(), user_role=role, conv_mode=mode,
                               event_cb=events.append, user_id="u1"))
    return out


@pytest.mark.parametrize("text", [
    "Work on the project in {root}\\shop and fix the cart total bug",
    "Trabalha no projeto em {root}\\shop e corrige o erro do total do carrinho",
    "In {root}/shop add a health endpoint and run the tests.",
    'Refactor the checkout in "{root}\\my shop", please',
])
def test_a_message_naming_an_allowed_folder_reaches_code_task(setup, text):
    orch, root, calls = setup
    out = _turn(orch, text.format(root=root))
    assert len(calls) == 1 and calls[0]["task"] == text.format(root=root)
    assert calls[0]["project_dir"].lower().endswith("shop")
    assert "Fixed it." in out and "code session" not in out


@pytest.mark.parametrize("mode", ["collaborate", "task"])
def test_modes_that_return_early_do_not_swallow_it(setup, mode):
    orch, root, calls = setup
    _turn(orch, f"fix the cart in {root}\\shop", mode=mode)
    assert len(calls) == 1


def test_a_pending_task_plan_does_not_swallow_a_terminal_line(setup):
    orch, root, calls = setup
    ctx = Context()
    ctx.set("pending_plan", ["something"])
    token = cp.CODE_FOLLOWUP.set({"project_dir": str(root / "shop")})
    try:
        _turn(orch, "now run the tests", mode="task", ctx=ctx)
    finally:
        cp.CODE_FOLLOWUP.reset(token)
    assert len(calls) == 1 and calls[0]["task"] == "now run the tests"


def test_a_folder_outside_the_roots_is_not_intercepted(setup, tmp_path):
    _, _, _ = setup
    (tmp_path / "elsewhere").mkdir()
    assert cp.project_in(f"fix {tmp_path / 'elsewhere'}") is None
    assert cp.project_in("no folder named here at all") is None


def test_with_no_roots_nothing_is_intercepted(monkeypatch, tmp_path):
    monkeypatch.setattr(cp._cfg, "get", lambda k, d=None: [] if k == "code_project_roots" else d)
    (tmp_path / "p").mkdir()
    assert cp.project_in(f"work on {tmp_path / 'p'}") is None


def test_standard_role_naming_a_folder_gets_a_refusal_not_a_run(setup):
    orch, root, calls = setup
    out = _turn(orch, f"fix the cart in {root}\\shop", role="standard")
    assert calls == [] and "not available" in out
