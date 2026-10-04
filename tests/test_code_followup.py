"""A line typed into the /face terminal goes to Claude Code in that project,
on a deterministic path: same floors as the tool loop (RBAC, policy deny,
approval every time), and nothing runs when the person says no."""
import asyncio

import pytest

from suni import approval
from suni.core.orchestrator import Orchestrator


class _Registry:
    def __init__(self):
        self.calls = []

    def names(self):
        return ["code_task"]

    async def execute(self, name, args):
        self.calls.append((name, args))
        return "Claude Code finished in C:/p after 3s.\n\nDone.\n\n[code session: abc-123]"


class _Stub:
    def __init__(self):
        self.registry = _Registry()


def _run(stub, *, role="admin", event_cb=lambda e: None, readonly=False, text="add a test"):
    return asyncio.run(Orchestrator._handle_code_followup(
        stub, text, {"project_dir": "C:/p"}, "u1", role, event_cb, readonly))


@pytest.fixture
def decision(monkeypatch):
    box = {"value": "allow", "asked": []}

    async def fake(tool, args, user_id, event_cb, risk=None):
        box["asked"].append((tool, args))
        return box["value"]
    monkeypatch.setattr(approval, "request_approval", fake)
    return box


def test_approved_runs_the_typed_line_and_hides_the_session_id(decision):
    stub = _Stub()
    msg = _run(stub)
    assert stub.registry.calls == [("code_task", {"project_dir": "C:/p", "task": "add a test",
                                                  "new_session": False})]
    assert decision["asked"][0][0] == "code_task"
    assert "Done." in msg.content and "code session" not in msg.content


def test_denied_runs_nothing(decision):
    decision["value"] = "deny"
    stub = _Stub()
    msg = _run(stub)
    assert stub.registry.calls == [] and "did not run" in msg.content


def test_standard_role_is_refused_before_any_card(decision):
    stub = _Stub()
    msg = _run(stub, role="standard")
    assert decision["asked"] == [] and stub.registry.calls == []
    assert "not available" in msg.content


def test_read_only_mode_is_refused(decision):
    stub = _Stub()
    _run(stub, readonly=True)
    assert decision["asked"] == [] and stub.registry.calls == []


def test_no_live_approver_is_refused(decision):
    stub = _Stub()
    msg = _run(stub, event_cb=None)
    assert decision["asked"] == [] and stub.registry.calls == []
    assert "live session" in msg.content


def test_followup_context_is_off_by_default():
    from suni.tools.code_project import CODE_FOLLOWUP
    assert CODE_FOLLOWUP.get() is None
