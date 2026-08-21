"""
Non-destructive updates from the upstream repository.

The property this rests on, verified rather than assumed: nothing under
memory/, logs/, files/, certs/, backups/ or .env is tracked by git. Every one
returns zero tracked files. So a fast-forward pull physically cannot overwrite
user data — the update surface is code, and only code.

Everything else here follows from one rule: **refuse rather than rescue.** If
the working tree is dirty, or there are local commits, or the pull would need a
merge, the update stops and says why. The alternative — stashing, merging,
resetting — hides a user's work and calls it success. An update that cannot run
is recoverable; an update that quietly discards local changes is not.

The updater does NOT restart the server. It reports that a restart is required
and leaves that to the operator, who knows what else is running.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# Directories a pull must never touch. Asserted at preflight rather than trusted:
# a .gitignore edit upstream could start tracking one of these, and the check
# costs nothing.
PROTECTED = ("memory", "logs", "files", "certs", "backups", ".env")

# Modules whose SQLite schema migrates itself on connect (PRAGMA table_info +
# ALTER). The rest create tables if absent but cannot add a column to a table
# that already exists — so an upstream change there needs a manual migration and
# the operator has to be told, not reassured.
_SELF_MIGRATING = ("auth", "audit", "agents")


def _git(*args: str, cwd: Path | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd or ROOT),
                           capture_output=True, text=True, timeout=180)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "git is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return 124, "git timed out"


def _is_repo(root: Path) -> bool:
    return _git("rev-parse", "--git-dir", cwd=root)[0] == 0


def status(root: Path | None = None) -> dict[str, Any]:
    """Where this install stands relative to upstream. Read-only."""
    root = root or ROOT
    out: dict[str, Any] = {
        "is_repo": False, "remote": "", "branch": "", "current": "",
        "behind": 0, "ahead": 0, "dirty": [], "can_update": False,
        "reason": "", "changes": [],
    }
    if not _is_repo(root):
        out["reason"] = "not a git checkout — update by replacing the files manually"
        return out
    out["is_repo"] = True
    out["remote"] = _git("remote", "get-url", "origin", cwd=root)[1]
    out["branch"] = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)[1]
    out["current"] = _git("rev-parse", "--short", "HEAD", cwd=root)[1]

    rc, _ = _git("fetch", "origin", "--quiet", cwd=root)
    if rc != 0:
        out["reason"] = "could not reach the remote"
        return out

    rc, counts = _git("rev-list", "--left-right", "--count",
                      f"origin/{out['branch']}...HEAD", cwd=root)
    if rc == 0 and counts:
        parts = counts.split()
        if len(parts) == 2:
            out["behind"], out["ahead"] = int(parts[0]), int(parts[1])

    dirty = _git("status", "--porcelain", cwd=root)[1]
    # Porcelain is "XY <path>", but the separator is not reliably one space —
    # a fixed slice ate the first character and reported "eb.py" for web.py.
    # A message whose whole job is naming the blocking file must name it right.
    out["dirty"] = [ln[2:].strip() for ln in dirty.splitlines() if ln.strip()][:20]

    if out["behind"]:
        out["changes"] = _git(
            "log", "--oneline", "--no-decorate", f"HEAD..origin/{out['branch']}",
            cwd=root)[1].splitlines()[:30]

    out["can_update"], out["reason"] = _decide(out)
    return out


def _decide(s: dict[str, Any]) -> tuple[bool, str]:
    """Whether an update may proceed, and the reason if not.

    Each refusal names what to do about it. "Cannot update" with no next step is
    the same as a silent failure from the operator's side.
    """
    if not s["is_repo"]:
        return False, "not a git checkout"
    if not s["remote"]:
        return False, "no 'origin' remote is configured"
    if s["dirty"]:
        return False, (f"{len(s['dirty'])} file(s) modified locally "
                       f"({', '.join(s['dirty'][:3])}…). Commit or discard them "
                       f"first — an update will not overwrite your changes.")
    if s["ahead"]:
        return False, (f"{s['ahead']} local commit(s) not on the remote. A "
                       f"fast-forward would lose them; push or remove them first.")
    if not s["behind"]:
        return False, "already up to date"
    return True, ""


def _protected_are_untracked(root: Path) -> list[str]:
    """Any protected directory that has become tracked upstream.

    The whole non-destructive claim depends on user data being invisible to git.
    A .gitignore change upstream could quietly break that, so it is checked
    before every update rather than assumed once.
    """
    bad = []
    for d in PROTECTED:
        rc, out = _git("ls-files", d, cwd=root)
        if rc == 0 and out.strip():
            bad.append(d)
    return bad


def requirements_changed(root: Path | None = None) -> bool:
    """Whether the incoming range touches dependencies.

    Reported, never acted on: installing packages into someone's environment
    without asking is its own kind of destructive.
    """
    root = root or ROOT
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)[1]
    rc, out = _git("diff", "--name-only", f"HEAD..origin/{branch}", cwd=root)
    if rc != 0:
        return False
    return any(n.strip().startswith("requirements") for n in out.splitlines())


def schema_risk(root: Path | None = None) -> list[str]:
    """Modules the update touches whose schema does NOT migrate itself.

    auth/audit/agents add missing columns on connect. The others create tables
    if absent but cannot alter one that exists, so a new column upstream fails
    against an existing database — the exact break seen when agent budgets were
    added. Naming the files lets an operator look before restarting.
    """
    root = root or ROOT
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)[1]
    rc, out = _git("diff", "--name-only", f"HEAD..origin/{branch}", cwd=root)
    if rc != 0:
        return []
    risky = []
    for name in out.splitlines():
        n = name.strip()
        if not n.startswith("suni/") or not n.endswith(".py"):
            continue
        stem = Path(n).stem
        if stem in _SELF_MIGRATING:
            continue
        # Only modules that own a database are interesting here.
        f = root / n
        try:
            if "CREATE TABLE" in f.read_text(encoding="utf-8", errors="ignore"):
                risky.append(n)
        except OSError:
            pass
    return risky


def apply(root: Path | None = None, backup: bool = True) -> dict[str, Any]:
    """Fast-forward to upstream, after taking a backup.

    Returns a result describing what happened and what the operator must still
    do. Never restarts the server: the process running this is the process being
    replaced, and whoever is watching knows what else depends on it.
    """
    root = root or ROOT
    result: dict[str, Any] = {
        "ok": False, "from": "", "to": "", "backup": "", "restart_required": False,
        "reinstall_deps": False, "schema_review": [], "message": "",
    }
    s = status(root)
    if not s["can_update"]:
        result["message"] = s["reason"]
        return result

    leaked = _protected_are_untracked(root)
    if leaked:
        result["message"] = (
            f"Refusing: {', '.join(leaked)} is tracked by git in this checkout, so "
            f"an update could overwrite instance data. Fix .gitignore first.")
        return result

    # Dependency and schema notes are gathered BEFORE the pull, while the
    # incoming range is still expressible as HEAD..origin.
    deps = requirements_changed(root)
    risky = schema_risk(root)

    if backup:
        try:
            from . import backup as _backup
            # Uploads and the FAISS index are excluded deliberately: both can be
            # very large, neither is destroyed by a code update, and a backup so
            # slow that people skip it protects nothing.
            result["backup"] = _backup.create()
        except Exception as exc:      # noqa: BLE001
            result["message"] = f"Backup failed, so nothing was changed: {exc}"
            return result

    result["from"] = s["current"]
    rc, out = _git("pull", "--ff-only", "origin", s["branch"], cwd=root)
    if rc != 0:
        result["message"] = (f"Pull failed and the checkout is unchanged: {out[:300]}")
        return result

    result["to"] = _git("rev-parse", "--short", "HEAD", cwd=root)[1]
    result["ok"] = True
    result["restart_required"] = True
    result["reinstall_deps"] = deps
    result["schema_review"] = risky
    bits = [f"Updated {result['from']} → {result['to']}."]
    if deps:
        bits.append("Dependencies changed — run: pip install -r requirements.txt")
    if risky:
        bits.append("These modules own databases and do not migrate themselves; "
                    "check for new columns before relying on them: "
                    + ", ".join(risky))
    bits.append("Restart SUNI to load the new code.")
    result["message"] = " ".join(bits)
    return result


def rollback(to_commit: str, root: Path | None = None) -> dict[str, Any]:
    """Return the CODE to a previous commit. Instance data is never touched.

    Deliberately narrow: this resets tracked files and nothing else. If the
    update also installed dependencies, those stay — reinstalling the old set is
    the operator's call, because guessing wrong there breaks more than it fixes.
    """
    root = root or ROOT
    if not to_commit or not to_commit.strip():
        return {"ok": False, "message": "no commit given"}
    rc, out = _git("cat-file", "-e", f"{to_commit}^{{commit}}", cwd=root)
    if rc != 0:
        return {"ok": False, "message": f"unknown commit {to_commit!r}"}
    rc, out = _git("reset", "--hard", to_commit, cwd=root)
    if rc != 0:
        return {"ok": False, "message": f"rollback failed: {out[:200]}"}
    return {"ok": True, "restart_required": True,
            "message": f"Code reset to {to_commit}. Instance data untouched. "
                       f"Restart SUNI, and reinstall dependencies if the update "
                       f"had changed them."}
