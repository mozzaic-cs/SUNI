"""code_task: Claude Code working inside a real project folder.

The flag set itself was measured against the real CLI (see the module
docstring); these tests pin everything SUNI decides around it: where it may
run, who may run it, that it is approved every time, what the terminal is
told, and which file changes are credited to the run.
"""
import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from suni import approval
from suni.tools import code_project as cp

FIXTURE = Path(__file__).parent / "fixtures" / "cc_stream_code_task.jsonl"


@pytest.fixture
def cfg(monkeypatch):
    values: dict = {}
    real_get = cp._cfg.get

    def fake_get(key, default=None):
        return values[key] if key in values else real_get(key, default)

    monkeypatch.setattr(cp._cfg, "get", fake_get)
    return values


# ── where it may run ─────────────────────────────────────────────────────────

def test_no_roots_means_off(cfg, tmp_path):
    cfg["code_project_roots"] = []
    real, why = cp.resolve_project(str(tmp_path))
    assert real is None and "No project folders are allowed" in why


def test_inside_a_root_is_allowed(cfg, tmp_path):
    proj = tmp_path / "shop"
    proj.mkdir()
    cfg["code_project_roots"] = [str(tmp_path)]
    real, why = cp.resolve_project(str(proj))
    assert real == os.path.realpath(proj) and why == ""


def test_dotdot_cannot_walk_out(cfg, tmp_path):
    root = tmp_path / "projects"
    (root / "shop").mkdir(parents=True)
    (tmp_path / "secret").mkdir()
    cfg["code_project_roots"] = [str(root)]
    real, why = cp.resolve_project(str(root / "shop" / ".." / ".." / "secret"))
    assert real is None and "outside" in why


def test_a_sibling_sharing_the_prefix_is_not_inside(cfg, tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj2").mkdir()
    cfg["code_project_roots"] = [str(tmp_path / "proj")]
    real, _ = cp.resolve_project(str(tmp_path / "proj2"))
    assert real is None


def test_a_file_is_not_a_project(cfg, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    cfg["code_project_roots"] = [str(tmp_path)]
    real, why = cp.resolve_project(str(f))
    assert real is None and "not a folder" in why


# ── what Claude Code is allowed ──────────────────────────────────────────────

def test_allowed_tools_never_grant_bare_write_edit_or_shell(cfg):
    cfg["code_allowed_commands"] = []
    rules = cp.allowed_tools_arg().split(",")
    # measured: a bare Write rule let the CLI write OUTSIDE the project
    for bare in ("Write", "Edit", "MultiEdit", "Bash", "PowerShell"):
        assert bare not in rules
    assert {"Read", "Glob", "Grep"} <= set(rules)
    # every shell rule has its PowerShell twin (Windows uses PowerShell)
    bash = {r[5:-1] for r in rules if r.startswith("Bash(")}
    ps = {r[11:-1] for r in rules if r.startswith("PowerShell(")}
    assert bash and bash == ps


def test_a_command_with_a_comma_is_dropped(cfg):
    cfg["code_allowed_commands"] = ["git status:*", "rm -rf /,echo"]
    assert "rm -rf" not in cp.allowed_tools_arg()


def test_preview_names_the_folder_commands_and_project_allows(cfg, tmp_path):
    proj = tmp_path / "shop"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(make deploy)"]}}))
    cfg["code_project_roots"] = [str(tmp_path)]
    text = approval.build_preview("code_task", {"project_dir": str(proj), "task": "fix the cart"})
    assert os.path.realpath(proj) in text
    assert "npm test" in text and "executes the project's own code" in text
    assert "Bash(make deploy)" in text            # the project's own allows are shown
    assert text.rstrip().endswith("fix the cart")


def test_preview_says_when_it_will_be_refused(cfg, tmp_path):
    cfg["code_project_roots"] = []
    text = approval.build_preview("code_task", {"project_dir": str(tmp_path), "task": "x"})
    assert "will be refused" in text


# ── approved every time ─────────────────────────────────────────────────────

def test_code_task_is_never_trusted(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                    # trust rules persist under memory/
    uid = "u_never_trusted"
    approval.add_trust_rule(uid, "code_task", "*")
    assert approval.is_trusted(uid, "code_task", {"project_dir": "C:/x", "task": "y"}) is False
    assert "code_task" not in approval.list_trust_rules(uid)
    assert approval.is_consequential("code_task")


# ── who and when ─────────────────────────────────────────────────────────────

def _run_handler(role, event_cb, **kw):
    from suni.tools.agent_tool import CURRENT_ROLE
    from suni.tools.claude_code_advanced import EVENT_CB_CTX

    async def go():
        r_tok, e_tok = CURRENT_ROLE.set(role), EVENT_CB_CTX.set(event_cb)
        try:
            return await cp.handler(**kw)
        finally:
            CURRENT_ROLE.reset(r_tok)
            EVENT_CB_CTX.reset(e_tok)
    return asyncio.run(go())


@pytest.mark.parametrize("role", ["standard", "read-only"])
def test_standard_and_read_only_never_get_it(role, cfg, tmp_path):
    cfg["code_project_roots"] = [str(tmp_path)]
    out = _run_handler(role, lambda e: None, project_dir=str(tmp_path), task="x")
    assert "not available" in out


def test_without_a_live_approver_it_refuses_at_once(cfg, tmp_path):
    cfg["code_project_roots"] = [str(tmp_path)]
    out = _run_handler("admin", None, project_dir=str(tmp_path), task="x")
    assert "live session" in out


def test_resume_latest_is_admin_only(cfg, tmp_path):
    cfg["code_project_roots"] = [str(tmp_path)]
    out = _run_handler("power-user", lambda e: None, project_dir=str(tmp_path),
                       task="x", resume_latest=True)
    assert "Only an admin" in out


def test_outside_the_roots_is_refused_before_anything_runs(cfg, tmp_path, monkeypatch):
    (tmp_path / "allowed").mkdir()
    (tmp_path / "other").mkdir()
    cfg["code_project_roots"] = [str(tmp_path / "allowed")]

    async def boom(*a, **k):
        raise AssertionError("the CLI must not start")
    monkeypatch.setattr("suni.tools.claude_code_advanced._run_claude_stream", boom)
    out = _run_handler("admin", lambda e: None, project_dir=str(tmp_path / "other"), task="x")
    assert "outside" in out


# ── the terminal feed, from a real captured run ─────────────────────────────

def test_parser_on_a_real_stream():
    events = []
    p = cp.StreamParser(events.append, r"C:\work\demo\proj", "run1")
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        p.feed(line)
    kinds = [e["kind"] for e in events]
    tools = [(e["name"], e["summary"]) for e in events if e["kind"] == "tool"]
    assert tools[0] == ("Write", "inside.txt")             # relative to the project
    assert ("PowerShell", "git status --short") in tools
    assert kinds.count("denied") == 2                      # outside write + redirect
    first_result = next(e for e in events if e["kind"] == "result")
    assert first_result["ok"] is True and first_result["added"] == 1
    assert any(e["kind"] == "result" and not e["ok"] for e in events)
    assert p.session_id and p.result.get("type") == "result"
    assert any("inside.txt" in t for t in p.touched)
    assert all(e["type"] == "term" and e["run"] == "run1" for e in events)


def test_secrets_are_redacted_before_the_terminal():
    events = []
    p = cp.StreamParser(events.append, "C:/w", "r")
    p.tools["t1"] = "Bash"
    p.feed(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": "ANTHROPIC_API_KEY=sk-ant-api03-" + "a" * 90}]}}))
    shown = events[0]["text"]
    assert "a" * 90 not in shown


# ── which changes belong to the run ─────────────────────────────────────────

def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_changes_credit_only_the_run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for name in ("kept.txt", "mine.txt", "theirs.txt"):
        (repo / name).write_text("v1\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")

    (repo / "mine.txt").write_text("developer's uncommitted edit\n")   # dirty BEFORE
    before = cp.snapshot(str(repo))

    (repo / "theirs.txt").write_text("v2 by the run\n")                # the run edits
    (repo / "new.txt").write_text("created by the run\n")              # the run creates

    got = {c["path"]: c for c in cp.changes_since(str(repo), before, set())}
    assert "mine.txt" not in got                 # dirty before, untouched by the run
    assert "kept.txt" not in got
    assert got["theirs.txt"]["status"] == "edited" and not got["theirs.txt"]["was_dirty"]
    assert got["new.txt"]["status"] == "new"


def test_a_dirty_file_the_run_also_edits_is_flagged(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("v1\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    (repo / "a.txt").write_text("dev edit\n")
    before = cp.snapshot(str(repo))
    (repo / "a.txt").write_text("dev edit\nplus the run\n")
    got = cp.changes_since(str(repo), before, set())
    assert got and got[0]["path"] == "a.txt" and got[0]["was_dirty"] is True


def test_outside_git_the_touched_files_are_the_report(tmp_path):
    got = cp.changes_since(str(tmp_path), None, {str(tmp_path / "x.py")})
    assert [c["path"] for c in got] == ["x.py"]


def test_a_secret_split_across_streamed_chunks_is_still_redacted():
    """Each chunk alone looks harmless; only the whole block is a secret.
    The parser re-sends the whole block, redacted, every time."""
    events = []
    p = cp.StreamParser(events.append, "C:/w", "r")
    key = "sk-ant-api03-" + "Q" * 60
    chunks = ["The key in .env is ANTHROPIC_API_KEY=", key[:20], key[20:]]
    p.feed(json.dumps({"type": "stream_event", "event": {"type": "content_block_start"}}))
    for c in chunks:
        p.feed(json.dumps({"type": "stream_event", "event": {
            "type": "content_block_delta", "delta": {"type": "text_delta", "text": c}}}))
    shown = [e["text"] for e in events if e["kind"] == "text"]
    assert shown and all("Q" * 40 not in t for t in shown)
    assert events[0].get("new") is True and not events[-1].get("new")


def test_the_card_shows_settings_that_widen_the_run(cfg, tmp_path):
    proj = tmp_path / "shop"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"additionalDirectories": ["D:/shared"], "defaultMode": "bypassPermissions"},
        "hooks": {"PostToolUse": []}}))
    cfg["code_project_roots"] = [str(tmp_path)]
    text = cp.preview({"project_dir": str(proj), "task": "x"})
    assert "OUTSIDE the project" in text and "D:/shared" in text
    assert "PostToolUse" in text and "bypassPermissions" in text
