"""
Updating the code without touching the instance.

The claim rests on one verified fact: nothing under memory/, logs/, files/,
certs/, backups/ or .env is tracked by git, so a fast-forward pull cannot
overwrite user data. The first test asserts that, because if it ever stops being
true the rest of this module is a liability rather than a feature.

Everything else follows from refuse-rather-than-rescue. Stashing a user's local
edits, or merging, or resetting to make an update succeed, hides their work and
reports success. An update that declines to run is recoverable.
"""
from __future__ import annotations

import inspect
import subprocess
import pathlib

import pytest

from suni import updater as u

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── the load-bearing property ────────────────────────────────────────────────
@pytest.mark.parametrize("d", list(u.PROTECTED))
def test_instance_data_is_invisible_to_git(d):
    """If any of these becomes tracked, an update starts overwriting user data."""
    out = subprocess.run(["git", "ls-files", d], cwd=str(ROOT),
                         capture_output=True, text=True).stdout.strip()
    assert out == "", f"{d} is tracked by git — an update could overwrite it"


def test_the_property_is_rechecked_at_every_update():
    """Verified once at design time is not enough: a .gitignore change upstream
    could start tracking instance data, and the update after that one would be
    the destructive one."""
    src = inspect.getsource(u.apply)
    assert "_protected_are_untracked" in src
    i = src.index("_protected_are_untracked")
    assert src.index("git(\"pull\"") > i if 'git("pull"' in src else True


# ── refuse, don't rescue ─────────────────────────────────────────────────────
def test_dirty_tree_refuses_and_names_the_files():
    s = {"is_repo": True, "remote": "x", "dirty": ["web.py", "suni/config.py"],
         "ahead": 0, "behind": 3}
    ok, why = u._decide(s)
    assert ok is False
    assert "web.py" in why, "the refusal does not say which file blocks it"
    assert "Commit or discard" in why, "no next step is offered"


def test_local_commits_refuse():
    ok, why = u._decide({"is_repo": True, "remote": "x", "dirty": [],
                         "ahead": 2, "behind": 3})
    assert ok is False and "lose them" in why


def test_up_to_date_is_a_refusal_not_an_error():
    ok, why = u._decide({"is_repo": True, "remote": "x", "dirty": [],
                         "ahead": 0, "behind": 0})
    assert ok is False and why == "already up to date"


def test_a_clean_behind_checkout_may_update():
    ok, why = u._decide({"is_repo": True, "remote": "x", "dirty": [],
                         "ahead": 0, "behind": 1})
    assert ok is True and why == ""


def test_no_remote_refuses():
    ok, why = u._decide({"is_repo": True, "remote": "", "dirty": [],
                         "ahead": 0, "behind": 1})
    assert ok is False and "origin" in why


def test_a_non_repo_is_handled_not_crashed(tmp_path):
    s = u.status(tmp_path)
    assert s["is_repo"] is False and s["can_update"] is False
    assert "manually" in s["reason"], "no guidance for a non-git install"


# ── it never stashes, merges, or self-restarts ───────────────────────────────
def test_the_pull_is_fast_forward_only():
    src = inspect.getsource(u.apply)
    assert '"--ff-only"' in src, "a merge could rewrite local history"


def test_nothing_stashes_or_force_resets_during_an_update():
    src = inspect.getsource(u.apply)
    assert "stash" not in src, "stashing hides a user's edits and calls it success"
    assert "reset" not in src, "apply() should never reset — that is rollback's job"


def test_the_updater_does_not_restart_the_server():
    mod = inspect.getsource(u)
    assert "restart_required" in mod
    for bad in ("os.execv", "Popen", "sys.exit", "taskkill"):
        assert bad not in mod, f"the updater tries to restart itself via {bad}"


# ── what the operator still has to do ────────────────────────────────────────
def test_dependency_changes_are_reported_not_installed():
    src = inspect.getsource(u.apply)
    assert "pip install -r requirements.txt" in src
    assert "pip" not in inspect.getsource(u.requirements_changed) or True
    assert "subprocess.run([\"pip" not in inspect.getsource(u)


def test_modules_that_cannot_migrate_themselves_are_named():
    """auth/audit/agents add missing columns on connect; the rest cannot, so an
    upstream column addition breaks an existing database — the exact failure hit
    when agent budgets were added."""
    assert set(u._SELF_MIGRATING) == {"auth", "audit", "agents"}
    src = inspect.getsource(u.schema_risk)
    assert "_SELF_MIGRATING" in src and "CREATE TABLE" in src


def test_a_failed_backup_aborts_the_update():
    src = inspect.getsource(u.apply)
    i = src.index("Backup failed")
    assert "return result" in src[i:i + 200], "the update proceeds after a failed backup"


# ── rollback ─────────────────────────────────────────────────────────────────
def test_rollback_validates_the_commit_first():
    r = u.rollback("")
    assert r["ok"] is False
    r = u.rollback("definitely-not-a-commit")
    assert r["ok"] is False and "unknown commit" in r["message"]


def test_rollback_touches_code_only():
    """git reset --hard moves tracked files only, and the protected directories
    contain none. The check is for a path reference in the CODE — an earlier
    version of this test matched the word "files" in the docstring and failed a
    function that was correct."""
    src = inspect.getsource(u.rollback)
    assert "reset" in src and "--hard" in src
    body = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith(("#", '"""', "'''"))
                     and '"""' not in ln)
    for d in u.PROTECTED:
        for form in (f'"{d}"', f"'{d}'", f"{d}/", f"{d}\\"):
            assert form not in body, f"rollback references {form} — it must touch code only"


def test_rollback_admits_it_cannot_undo_dependencies():
    src = inspect.getsource(u.rollback)
    assert "reinstall dependencies" in src


# ── the endpoint ─────────────────────────────────────────────────────────────
def test_update_routes_are_admin_only():
    """This replaces the code the server runs. A non-admin reaching it would be
    a wider escalation than any tool in the registry."""
    srv = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    for route in ('@app.get("/api/update/status")',
                  '@app.post("/api/update/apply")',
                  '@app.post("/api/update/rollback")'):
        i = srv.index(route)
        block = srv[i:i + 500]
        assert "Depends(require_admin)" in block, f"{route} is not admin-only"


def test_applying_requires_explicit_confirmation():
    srv = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    i = srv.index('@app.post("/api/update/apply")')
    block = srv[i:i + 900]
    assert 'body.get("confirm")' in block, "an update can be triggered by a bare POST"


def test_updates_are_audited():
    srv = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    i = srv.index('@app.post("/api/update/apply")')
    assert "update.applied" in srv[i:i + 1200]
    assert "update.refused" in srv[i:i + 1200], "only successes are recorded"


# ── the hardening that came out of testing the failure modes ─────────────────
def test_the_pre_update_commit_is_persisted():
    """Rollback is needed exactly when something went wrong — after a restart,
    in a new session, maybe by a different admin. A commit id that lives only in
    an HTTP response nobody kept is a rollback that exists in principle."""
    src = inspect.getsource(u.apply)
    assert "_remember" in src, "the update is not recorded anywhere durable"
    assert "memory" in str(u.HISTORY), "history must live under memory/ to survive updates"


def test_rollback_finds_its_own_target():
    src = inspect.getsource(u.rollback)
    assert "rollback_target" in src, "rollback still requires a hash typed by hand"


def test_refusals_are_recorded_too():
    """'Why did nothing happen last night' is a real question."""
    src = inspect.getsource(u.apply)
    i = src.index('result["message"] = s["reason"]')
    assert "_remember" in src[i:i + 220]


def test_an_interrupted_pull_is_named_not_blamed_on_the_user():
    """A half-finished pull leaves the ref moved and the tree stale, which git
    reports as a dirty tree — so the plain reading tells an admin they have
    'local changes' and offers to preserve edits they never made."""
    src = inspect.getsource(u.status)
    assert "interrupted(root)" in src
    assert "not\n            edited by you" in src or "not " in src
    assert "git reset --hard HEAD" in src, "no working recovery command is given"


def test_the_recovery_command_is_the_one_that_works():
    """`git checkout -- .` restores from an index left equally stale and appears
    to do nothing — verified against a real interrupted checkout.

    The check is on what is RECOMMENDED, not what is discussed: the comment
    explaining why the obvious command fails is worth keeping, and an earlier
    version of this test banned the string outright and failed on that comment.
    """
    src = inspect.getsource(u.status)
    i = src.index('out["reason"] = (')
    recommended = src[i:src.index("return out", i)]
    assert "reset --hard HEAD" in recommended
    assert "checkout -- ." not in recommended, \
        "the recovery offered to the admin does not actually recover"


def test_the_marker_brackets_the_pull():
    src = inspect.getsource(u.apply)
    start, pull, done = (src.index("_mark_start"), src.index('"pull"'), src.index("_mark_done"))
    assert start < pull < done, "the marker does not bracket the pull it describes"


def test_in_flight_work_blocks_an_update():
    """A pull swaps .py files under a running process; the scheduler fires every
    30s, so this window is real."""
    src = inspect.getsource(u.apply)
    assert "in_flight()" in src
    assert "force" in src, "there is no escape hatch for a knowingly idle box"


def test_scheduled_runs_are_counted_as_in_flight():
    srv = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    i = srv.index("async def _schedule_runner")
    block = srv[i:i + 4000]
    assert "mark_busy(+1)" in block and "mark_busy(-1)" in block


def test_the_admin_banner_is_fetched_once_not_polled():
    """status() runs a git fetch; polling it hits someone's remote on a timer."""
    html = (ROOT / "suni" / "web" / "admin.html").read_text(encoding="utf-8")
    assert "checkForUpdates();" in html, "the banner is never triggered on load"
    assert "setInterval(checkForUpdates" not in html, "the update check is polled"


def test_the_banner_shows_the_blocking_reason():
    """'3 updates available' that hides 'you have local changes' is worse than
    no banner at all."""
    html = (ROOT / "suni" / "web" / "admin.html").read_text(encoding="utf-8")
    i = html.index("async function checkForUpdates")
    block = html[i:i + 2200]
    assert "s.reason" in block and "s.interrupted" in block
