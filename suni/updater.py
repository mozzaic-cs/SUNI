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


# Where the pre-update commit is remembered. Under memory/ on purpose: it is
# untracked, so it survives the very update it describes. Held in a file rather
# than only returned to the caller because rollback is needed exactly when
# something went wrong — after a restart, in a new browser session, possibly by
# a different admin. A commit id that lives only in an HTTP response nobody kept
# is a rollback that exists in principle and not in practice.
HISTORY = Path("memory") / "update_history.json"
_HISTORY_KEEP = 20


def history(root: Path | None = None) -> list[dict[str, Any]]:
    """Past updates, newest first. Never raises: this is a convenience."""
    import json
    f = (root or ROOT) / HISTORY
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:      # noqa: BLE001 — absent or corrupt is simply "no history"
        return []


def _remember(root: Path, entry: dict[str, Any]) -> None:
    import json
    from datetime import datetime, timezone
    f = root / HISTORY
    entry = {"ts": datetime.now(timezone.utc).isoformat(), **entry}
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        rows = history(root)
        rows.insert(0, entry)
        f.write_text(json.dumps(rows[:_HISTORY_KEEP], indent=2), encoding="utf-8")
    except Exception:      # noqa: BLE001 — an update must not fail over bookkeeping
        pass


MARKER = Path("memory") / "update_in_progress.json"


def _mark_start(root: Path, frm: str, to: str) -> None:
    """Record that a pull is about to happen.

    An interrupted pull leaves the ref moved and the working tree stale. Git
    reports that as a dirty tree, so without this marker the next status() call
    tells the admin they have "local changes" and offers to preserve edits they
    never made — and the obvious recovery, `git checkout -- .`, restores from an
    index that is equally stale and appears to do nothing.
    """
    import json
    from datetime import datetime, timezone
    try:
        f = root / MARKER
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"from": frm, "to": to,
                                 "started": datetime.now(timezone.utc).isoformat()}),
                     encoding="utf-8")
    except Exception:      # noqa: BLE001
        pass


def _mark_done(root: Path) -> None:
    try:
        (root / MARKER).unlink(missing_ok=True)
    except Exception:      # noqa: BLE001
        pass


def interrupted(root: Path | None = None) -> dict[str, Any] | None:
    """Details of a pull that started and never finished, if any."""
    import json
    try:
        return json.loads((root or ROOT).joinpath(MARKER).read_text(encoding="utf-8"))
    except Exception:      # noqa: BLE001
        return None


def rollback_target(root: Path | None = None) -> str:
    """The commit the last successful update moved away from, or ''.

    This is what makes rollback usable without the admin having written a hash
    down before things went wrong.
    """
    for row in history(root):
        if row.get("ok") and row.get("from"):
            return str(row["from"])
    return ""


def version(root: Path | None = None) -> dict[str, str]:
    """What is actually running: the declared version and the exact commit.

    Both, because they answer different questions. The version says which
    release this is meant to be; the commit says what the files on disk actually
    are, which is the one that matters when something is behaving oddly and
    nobody is sure whether an update landed.
    """
    root = root or ROOT
    ver = ""
    try:
        for line in (root / "pyproject.toml").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("version"):
                ver = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    except Exception:      # noqa: BLE001
        pass
    commit = _git("rev-parse", "--short", "HEAD", cwd=root)[1] if _is_repo(root) else ""
    dirty = ""
    if commit:
        rc, out = _git("status", "--porcelain", cwd=root)
        if rc == 0 and out.strip():
            dirty = "modified"
    return {"version": ver, "commit": commit, "state": dirty}


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
        "reason": "", "changes": [], "interrupted": None,
        "reason_code": "", "reason_params": {},
    }
    if not _is_repo(root):
        out["reason"] = _REASON_EN["not_a_repo_manual"]
        out["reason_code"] = "not_a_repo_manual"
        return out
    out["is_repo"] = True
    out["remote"] = _git("remote", "get-url", "origin", cwd=root)[1]
    out["branch"] = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)[1]
    out["current"] = _git("rev-parse", "--short", "HEAD", cwd=root)[1]

    rc, _ = _git("fetch", "origin", "--quiet", cwd=root)
    if rc != 0:
        out["reason"] = _REASON_EN["unreachable"]
        out["reason_code"] = "unreachable"
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
    out["reason_code"], out["reason_params"] = _reason_meta(out)

    # An unfinished pull looks exactly like local edits. Say what it really is,
    # and give the recovery that works: `git checkout -- .` restores from an
    # index left just as stale, so it appears to do nothing at all.
    unfinished = interrupted(root)
    if unfinished and out["dirty"]:
        out["interrupted"] = unfinished
        out["can_update"] = False
        params = {"frm": unfinished.get("from", "?"), "to": unfinished.get("to", "?")}
        out["reason"] = _REASON_EN["interrupted"].format(**params)
        out["reason_code"] = "interrupted"
        out["reason_params"] = params
    return out


# The English text of every refusal, keyed by a stable code.
#
# The code is what makes a refusal translatable. These strings are built here,
# in Python, and shown in the admin panel, so a panel set to Portuguese used to
# render a wall of English at exactly the moment the operator needed to read it.
# status() therefore reports reason_code and reason_params alongside the prose,
# and the panel renders its own translation from those, falling back to this
# text when it has no translation for the code.
#
# Placeholders are named, never positional: word order differs between
# languages, and a translator must be free to move them.
_REASON_EN = {
    "not_a_repo": "not a git checkout",
    "no_remote": "no 'origin' remote is configured",
    "dirty": ("{n} file(s) modified locally ({files}…). Commit or discard them "
              "first — an update will not overwrite your changes."),
    "ahead": ("{n} local commit(s) not on the remote. A fast-forward would lose "
              "them; push or remove them first."),
    "uptodate": "already up to date",
    "unreachable": "could not reach the remote",
    "not_a_repo_manual": "not a git checkout — update by replacing the files manually",
    "interrupted": ("A previous update was interrupted (was moving {frm} → {to}). "
                    "The working tree is mid-checkout, not edited by you. Finish "
                    "it with: git reset --hard HEAD — then restart SUNI. Instance "
                    "data is untouched either way."),
}


# The outcome of an update, in the same translatable shape as the refusals.
#
# apply() reports a composite — what moved, what was backed up, what the
# operator must still do — so it emits a LIST of parts rather than one code.
# The panel translates each part and joins them; if any part has no translation
# it falls back to the English whole, because half a sentence in each language
# is worse than either.
_MSG_EN = {
    "in_flight": ("{n} request or scheduled run still in flight. Wait for it to "
                  "finish, or apply with force=true if you know the box is idle."),
    "leaked": ("Refusing: {paths} is tracked by git in this checkout, so an update "
               "could overwrite instance data. Fix .gitignore first."),
    "backup_failed": "Backup failed, so nothing was changed: {error}",
    "pull_failed": "Pull failed and the checkout is unchanged: {error}",
    "updated": "Updated {frm} → {to}.",
    # The coverage and the exclusions are spelled out here rather than passed in
    # as parameters: a parameter would carry English prose into a translated
    # sentence. test_the_backup_states_what_it_does_not_cover pins the wording.
    "backup_made": ("Backup {name} ({mb} MB) covers databases, config, agents, "
                    "schedules, skills and secrets; it excludes uploaded documents "
                    "and the FAISS index (rebuildable; unaffected by a code update)."),
    "deps_changed": "Dependencies changed — run: pip install -r requirements.txt",
    "schema_review": ("These modules own databases and do not migrate themselves; "
                      "check for new columns before relying on them: {modules}"),
    "restart": "Restart SUNI to load the new code.",
    "rb_no_commit": "no commit given, and no previous update is recorded",
    "rb_unknown": "unknown commit {commit}",
    "rb_failed": "rollback failed: {error}",
    "rb_ok": ("Code reset to {commit}. Instance data untouched. Restart SUNI, and "
              "reinstall dependencies if the update had changed them."),
}


def _msg(code: str, **params: Any) -> dict[str, Any]:
    """One translatable message part, plus its English rendering."""
    return {"code": code, "params": params}


def _render(parts: list[dict[str, Any]]) -> str:
    return " ".join(_MSG_EN[p["code"]].format(**p["params"]) for p in parts)


def _said(result: dict[str, Any], *parts: dict[str, Any]) -> dict[str, Any]:
    """Attach a message to a result in both forms — English and translatable."""
    result["message_parts"] = list(parts)
    result["message"] = _render(list(parts))
    return result


def _reason_meta(s: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The single branch ladder deciding why an update may not proceed.

    _decide() renders this to English rather than repeating the conditions, so
    the code and the prose cannot drift apart.
    """
    if not s["is_repo"]:
        return "not_a_repo", {}
    if not s["remote"]:
        return "no_remote", {}
    if s["dirty"]:
        return "dirty", {"n": len(s["dirty"]), "files": ", ".join(s["dirty"][:3])}
    if s["ahead"]:
        return "ahead", {"n": s["ahead"]}
    if not s["behind"]:
        return "uptodate", {}
    return "", {}


def _decide(s: dict[str, Any]) -> tuple[bool, str]:
    """Whether an update may proceed, and the reason if not.

    Each refusal names what to do about it. "Cannot update" with no next step is
    the same as a silent failure from the operator's side.
    """
    code, params = _reason_meta(s)
    if not code:
        return True, ""
    return False, _REASON_EN[code].format(**params)


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


# Set by the server while a request or scheduled run is in flight, so an update
# can decline to swap source files underneath one.
_BUSY: dict[str, int] = {"n": 0}


def mark_busy(delta: int) -> None:
    """Count in-flight work. Called by the server around runs; safe to ignore."""
    _BUSY["n"] = max(0, _BUSY["n"] + delta)


def in_flight() -> int:
    return _BUSY["n"]


def apply(root: Path | None = None, backup: bool = True,
          force: bool = False) -> dict[str, Any]:
    """Fast-forward to upstream, after taking a backup.

    Returns a result describing what happened and what the operator must still
    do. Never restarts the server: the process running this is the process being
    replaced, and whoever is watching knows what else depends on it.
    """
    root = root or ROOT
    result: dict[str, Any] = {
        "ok": False, "from": "", "to": "", "backup": "", "restart_required": False,
        "reinstall_deps": False, "schema_review": [], "message": "",
        "backup_covers": "", "backup_excludes": "", "backup_mb": 0,
        "message_parts": [], "reason_code": "", "reason_params": {},
    }
    s = status(root)
    if not s["can_update"]:
        # A refusal here is the same refusal status() reports, so it reuses the
        # same code rather than inventing a second vocabulary for it.
        result["message"] = s["reason"]
        result["reason_code"] = s.get("reason_code", "")
        result["reason_params"] = s.get("reason_params", {})
        _remember(root, {"ok": False, "from": s.get("current", ""),
                         "reason": s["reason"]})
        return result

    # A pull replaces .py files under a running process. Python has already read
    # what it imported, so a finished turn is unaffected — but a turn still
    # executing may import something new mid-flight and get the wrong half of an
    # update. The scheduler fires every 30s here, so this is a real window, not
    # a theoretical one. force=True is for an operator who knows the box is idle.
    if not force and in_flight():
        _said(result, _msg("in_flight", n=in_flight()))
        _remember(root, {"ok": False, "from": s.get("current", ""),
                         "reason": result["message"]})
        return result

    leaked = _protected_are_untracked(root)
    if leaked:
        _said(result, _msg("leaked", paths=", ".join(leaked)))
        return result

    # Dependency and schema notes are gathered BEFORE the pull, while the
    # incoming range is still expressible as HEAD..origin.
    deps = requirements_changed(root)
    risky = schema_risk(root)

    if backup:
        try:
            from . import backup as _backup
            # Uploads and the FAISS index are excluded deliberately: the index is
            # rebuildable, both can be enormous, and a backup slow enough that
            # people start skipping it protects nothing. An update also cannot
            # reach either of them — they are untracked, like all instance data.
            #
            # What it DOES cover is reported below rather than left to be
            # assumed. A backup trusted for more than it holds is worse than no
            # backup, because it is only discovered to be partial at the moment
            # it is needed.
            result["backup"] = _backup.create()
            result["backup_covers"] = ("databases, config, agents, schedules, "
                                       "skills and secrets")
            result["backup_excludes"] = ("uploaded documents and the FAISS index "
                                         "(rebuildable; unaffected by a code update)")
            try:
                _bp = root / "backups" / result["backup"]
                result["backup_mb"] = round(_bp.stat().st_size / 1048576, 1)
            except OSError:
                result["backup_mb"] = 0
        except Exception as exc:      # noqa: BLE001
            _said(result, _msg("backup_failed", error=exc))
            return result

    result["from"] = s["current"]
    _target = _git("rev-parse", "--short", f"origin/{s['branch']}", cwd=root)[1]
    _mark_start(root, s["current"], _target)
    rc, out = _git("pull", "--ff-only", "origin", s["branch"], cwd=root)
    _mark_done(root)
    if rc != 0:
        _said(result, _msg("pull_failed", error=out[:300]))
        return result

    result["to"] = _git("rev-parse", "--short", "HEAD", cwd=root)[1]
    result["ok"] = True
    result["restart_required"] = True
    result["reinstall_deps"] = deps
    result["schema_review"] = risky
    bits = [_msg("updated", frm=result["from"], to=result["to"])]
    if result.get("backup"):
        bits.append(_msg("backup_made", name=result["backup"],
                         mb=result.get("backup_mb", 0)))
    if deps:
        bits.append(_msg("deps_changed"))
    if risky:
        bits.append(_msg("schema_review", modules=", ".join(risky)))
    bits.append(_msg("restart"))
    _said(result, *bits)
    _remember(root, {"ok": True, "from": result["from"], "to": result["to"],
                     "backup": result["backup"], "deps": deps,
                     "schema_review": risky})
    return result


def rollback(to_commit: str, root: Path | None = None) -> dict[str, Any]:
    """Return the CODE to a previous commit. Instance data is never touched.

    Deliberately narrow: this resets tracked files and nothing else. If the
    update also installed dependencies, those stay — reinstalling the old set is
    the operator's call, because guessing wrong there breaks more than it fixes.
    """
    root = root or ROOT
    to_commit = (to_commit or "").strip() or rollback_target(root)
    if not to_commit:
        return _said({"ok": False}, _msg("rb_no_commit"))
    rc, out = _git("cat-file", "-e", f"{to_commit}^{{commit}}", cwd=root)
    if rc != 0:
        return _said({"ok": False}, _msg("rb_unknown", commit=repr(to_commit)))
    rc, out = _git("reset", "--hard", to_commit, cwd=root)
    if rc != 0:
        return _said({"ok": False}, _msg("rb_failed", error=out[:200]))
    return _said({"ok": True, "restart_required": True},
                 _msg("rb_ok", commit=to_commit))
