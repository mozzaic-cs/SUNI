"""code_task — SUNI hands a coding task to Claude Code inside a real project.

The older delegation tools could not do this. Measured 2026-10-03:

- `claude --print` with no permission flags denies EVERY write, so
  claude_code / claude_task could read a project but never change it.
- claude_task ran in SUNI's own folder, so a resumed session pointed at the
  wrong project; claude_code ran in the folder but returned no session id.

What this tool runs, and why each flag is there (all measured, see the
memory note on headless permissions):

  --permission-mode acceptEdits   edits INSIDE the folder go through; a write
                                  to ../outside.txt is refused
  --allowedTools Read,Glob,Grep + Bash(...)/PowerShell(...) patterns only
                                  bare Write/Edit here means "write anywhere";
                                  mutating commands not listed are refused
                                  (read-only ones like whoami still run)
  --setting-sources project       keeps the project's CLAUDE.md and its shared
                                  .claude/settings.json, drops the personal
                                  settings.local.json, whose accumulated allow
                                  rules SUNI's approval card cannot vouch for
  --strict-mcp-config             no personal claude.ai connectors

Every run is approved by a person first (approval.NEVER_TRUSTED keeps it out
of "always allow"), only inside folders an admin listed in
code_project_roots, one run per folder at a time, and never for the
standard or read-only roles whatever the role file says.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from .. import config as _cfg
from .registry import USER_ID_CTX

log = logging.getLogger("suni.code_task")

# Commands Claude Code may run that CHANGE things. Read-only commands run
# regardless. Each becomes a Bash(...) and a PowerShell(...) rule: on Windows
# it reaches for PowerShell, and a Bash-only list refused every test run.
# Running tests executes the project's own code — the approval card says so.
DEFAULT_COMMANDS = [
    "git status:*", "git diff:*", "git log:*", "git show:*",
    "npm test:*", "npm run test:*", "npm run lint:*", "npm run build:*",
    "pytest:*", "python -m pytest:*", "py -m pytest:*",
    "cargo test:*", "cargo build:*", "cargo check:*",
    "go test:*", "go build:*", "go vet:*",
    "dotnet test:*", "dotnet build:*",
]

# Roles that never get this, whatever memory/role_config.json says. That file
# stores whole blocked lists, so a tool added to rbac.DEFAULTS later is NOT
# blocked on an install that already saved one — measured on this box.
_NEVER_ROLES = {"standard", "read-only"}

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Set by /api/chat when the message was typed into a /face terminal:
# {"project_dir": ..., "new_session": bool}. The orchestrator then sends the
# text to Claude Code in that project instead of to the model.
from contextvars import ContextVar
CODE_FOLLOWUP: ContextVar = ContextVar("suni_code_followup", default=None)
_TERM_TEXT_MAX = 4000        # one tool result on screen
_locks: dict[str, asyncio.Lock] = {}


# ── where it may run ─────────────────────────────────────────────────────────

def _roots() -> list[str]:
    raw = _cfg.get("code_project_roots") or []
    if isinstance(raw, str):
        raw = [r for r in re.split(r"[\n;]", raw)]
    out = []
    for r in raw:
        r = str(r).strip()
        if r:
            out.append(os.path.normcase(os.path.realpath(os.path.expanduser(r))))
    return out


def resolve_project(path: str) -> tuple[str | None, str]:
    """(real path, "") when `path` is a folder inside an allowed root, else
    (None, reason). realpath resolves .., symlinks and junctions before the
    comparison, so neither can walk out of a root."""
    if not str(path or "").strip():
        return None, "No project folder was given."
    real = os.path.realpath(os.path.expanduser(str(path).strip()))
    if not os.path.isdir(real):
        return None, f"{real} is not a folder."
    roots = _roots()
    if not roots:
        return None, ("No project folders are allowed yet. An admin has to list "
                      "them under code_project_roots in the configuration.")
    nreal = os.path.normcase(real)
    for root in roots:
        try:
            if os.path.commonpath([nreal, root]) == root:
                return real, ""
        except ValueError:          # different drives on Windows
            continue
    return None, f"{real} is outside the folders allowed for coding work."


_PATH_RE = re.compile(r'"([^"\n]+)"|\'([^\'\n]+)\'|([A-Za-z]:[\\/][^\s"\'<>|?*]*|~?/[^\s"\'<>|?*]+)')


def project_in(text: str) -> str | None:
    """The first folder named in `text` that lies inside an allowed root, or
    None. Quoted paths may contain spaces; bare ones lose trailing
    punctuation. Only a path that resolves inside code_project_roots counts,
    so this never fires while coding work is off."""
    if not _roots():
        return None
    for m in _PATH_RE.finditer(text or ""):
        cand = (m.group(1) or m.group(2) or m.group(3) or "").strip().rstrip(".,;:!?)]}")
        if not cand or not re.search(r"[\\/]", cand):
            continue
        real, _ = resolve_project(cand)
        if real:
            return real
    return None


def _commands() -> list[str]:
    raw = _cfg.get("code_allowed_commands")
    cmds = raw if isinstance(raw, list) and raw else DEFAULT_COMMANDS
    # a comma would split the --allowedTools list in the wrong place
    return [str(c).strip() for c in cmds if str(c).strip() and "," not in str(c)]


def allowed_tools_arg() -> str:
    rules = ["Read", "Glob", "Grep"]
    for c in _commands():
        rules += [f"Bash({c})", f"PowerShell({c})"]
    return ",".join(rules)


def project_settings(real: str) -> dict:
    """What the project's shared .claude/settings.json adds to a run. It still
    loads under --setting-sources project, so the card must show anything in
    it that widens what Claude Code may do: extra allow rules, extra folders
    it may write to, hooks (which run commands), and a default mode."""
    try:
        d = json.loads((Path(real) / ".claude" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    perm = d.get("permissions") if isinstance(d.get("permissions"), dict) else {}
    hooks = d.get("hooks") if isinstance(d.get("hooks"), dict) else {}
    return {
        "allow": [str(a) for a in (perm.get("allow") or [])][:40],
        "dirs": [str(a) for a in (perm.get("additionalDirectories") or [])][:20],
        "hooks": sorted(str(k) for k in hooks)[:20],
        "mode": str(perm.get("defaultMode") or ""),
    }


def project_settings_allows(real: str) -> list[str]:
    return project_settings(real).get("allow", [])


def preview(args: dict) -> str:
    """The approval card: where, what it may do, and the task. Side-effect free."""
    real, why = resolve_project(args.get("project_dir", ""))
    lines = []
    if real:
        lines.append(f"Folder: {real}")
    else:
        lines.append(f"Folder: {args.get('project_dir', '')}  (will be refused: {why})")
    lines.append("Claude Code may edit files in this folder, and run:")
    lines.append("  " + ", ".join(c.replace(":*", "") for c in _commands()))
    lines.append("Running tests or builds executes the project's own code.")
    ps = project_settings(real) if real else {}
    if ps.get("allow"):
        lines.append("The project's .claude/settings.json also allows:")
        lines.append("  " + ", ".join(ps["allow"]))
    if ps.get("dirs"):
        lines.append("It also lets Claude Code work in these folders, OUTSIDE the project:")
        lines.append("  " + ", ".join(ps["dirs"]))
    if ps.get("hooks"):
        lines.append("It has hooks that run commands on: " + ", ".join(ps["hooks"]))
    if ps.get("mode"):
        lines.append(f"Its default permission mode ({ps['mode']}) is overridden by acceptEdits.")
    if args.get("resume_latest"):
        lines.append("Continues the most recent Claude Code session in this folder.")
    lines.append("")
    lines.append(str(args.get("task", "")))
    return "\n".join(lines)


# ── sessions, per user and folder ────────────────────────────────────────────

def _sessions_path(user_id: str) -> Path | None:
    if not _SAFE_ID.match(user_id or ""):
        return None
    return Path("memory") / "users" / user_id / "code_sessions.json"


def _load_sessions(user_id: str) -> dict:
    p = _sessions_path(user_id)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p else {}
    except (OSError, ValueError):
        return {}


def _save_session(user_id: str, real: str, sid: str) -> None:
    p = _sessions_path(user_id)
    if not p or not sid:
        return
    d = _load_sessions(user_id)
    d[os.path.normcase(real)] = {"session_id": sid, "at": int(time.time())}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=1), encoding="utf-8")
    except OSError as e:
        log.warning("[CODE] could not save session for %s: %s", real, e)


def last_session(user_id: str, real: str) -> str:
    return (_load_sessions(user_id).get(os.path.normcase(real)) or {}).get("session_id", "")


# ── what changed ─────────────────────────────────────────────────────────────

def _git(real: str, *args: str) -> tuple[int, str]:
    try:
        from .. import proc as _proc
        p = _proc.run(["git", "-C", real, *args], capture_output=True, timeout=20)
        return p.returncode, p.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _hash(path: str) -> str:
    try:
        st = os.stat(path)
        if st.st_size > 20_000_000:
            return f"size:{st.st_size}:{st.st_mtime_ns}"
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()
    except OSError:
        return "missing"


def _porcelain(real: str) -> list[str] | None:
    rc, out = _git(real, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if rc != 0:
        return None
    paths, parts, i = [], out.split("\0"), 0
    while i < len(parts):
        e = parts[i]
        if len(e) > 3:
            paths.append(e[3:])
            if e[0] in "RC":            # rename/copy: the next field is the old path
                i += 1
        i += 1
    return paths


def snapshot(real: str) -> dict | None:
    """{path: hash} for every file git reports dirty. None outside a repo."""
    paths = _porcelain(real)
    if paths is None:
        return None
    return {p: _hash(os.path.join(real, p)) for p in paths}


def changes_since(real: str, before: dict | None, touched: set[str]) -> list[dict]:
    """Files the RUN changed. A raw diff against HEAD would credit Claude Code
    with edits the developer had not committed yet, so compare against the
    snapshot taken before the run instead. Outside git, fall back to the
    files its own Edit/Write calls touched."""
    if before is None:
        return [{"path": os.path.relpath(p, real) if os.path.isabs(p) else p,
                 "status": "edited", "was_dirty": False} for p in sorted(touched)]
    after_paths = _porcelain(real) or []
    out = []
    for p in sorted(set(after_paths) | set(before)):
        now = _hash(os.path.join(real, p))
        if p in before and before[p] == now:
            continue                                  # untouched since the snapshot
        if p not in before and p not in after_paths:
            continue
        status = ("deleted" if now == "missing" else
                  "reverted" if p not in after_paths else
                  "edited")
        out.append({"path": p, "status": status, "was_dirty": p in before})
    if out:
        rc, num = _git(real, "diff", "--numstat", "--", *[c["path"] for c in out])
        stats = {}
        for line in num.splitlines():
            bits = line.split("\t")
            if len(bits) == 3:
                stats[bits[2]] = (bits[0], bits[1])
        for c in out:
            if c["path"] in stats:
                a, r = stats[c["path"]]
                # numstat prints "-" for binary files
                c["added"], c["removed"] = (int(a) if a.isdigit() else a,
                                            int(r) if r.isdigit() else r)
            elif c["status"] == "edited" and c["path"] not in stats:
                c["status"] = "new"
    return out


# ── the terminal feed ────────────────────────────────────────────────────────

def _redact(tool: str, text: str) -> str:
    """Terminal events leave mid-run, before the orchestrator's output guard
    sees anything, so secrets are redacted here: a `cat .env` must not land
    on the screen or in a log."""
    try:
        from .. import output_guard as _og
        text, _ = _og.scan(tool, text)
    except Exception:          # noqa: BLE001 — never lose the line over redaction
        pass
    if len(text) > _TERM_TEXT_MAX:
        text = text[:_TERM_TEXT_MAX] + f"\n… ({len(text) - _TERM_TEXT_MAX} more characters)"
    return text


def _short_path(p: str, real: str) -> str:
    try:
        rel = os.path.relpath(p, real)
        return p if rel.startswith("..") else rel
    except ValueError:
        return p


def tool_summary(name: str, inp: dict, real: str) -> str:
    inp = inp or {}
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        return _short_path(str(inp.get("file_path") or inp.get("notebook_path") or ""), real)
    if name in ("Bash", "PowerShell"):
        return str(inp.get("command", ""))[:300]
    if name in ("Glob", "Grep"):
        return str(inp.get("pattern", ""))[:200]
    if name in ("WebFetch",):
        return str(inp.get("url", ""))
    if name in ("Task", "Agent"):
        return str(inp.get("description", ""))[:200]
    for v in inp.values():
        if isinstance(v, str) and v:
            return v[:200]
    return ""


class StreamParser:
    """Turns Claude Code's stream-json lines into terminal events.

    Built and tested against a captured transcript (tests/fixtures). Text
    streams as deltas; tool calls come from the complete assistant messages,
    which carry the full input; results come from the user messages that
    follow, with Edit/Write carrying a structured patch for the diff.
    """

    def __init__(self, emit, real: str, run_id: str):
        self.emit, self.real, self.run = emit, real, run_id
        self.tools: dict[str, str] = {}          # tool_use_id -> tool name
        self.touched: set[str] = set()
        self.denials: list[dict] = []
        self.result: dict = {}
        self.session_id = ""
        self._delta_msgs: set[str] = set()
        self._cur_msg = ""
        # The text block being streamed. Each delta re-sends the WHOLE block,
        # redacted, and the terminal replaces what it shows: a secret split
        # across two deltas is invisible to a per-chunk scan.
        self._block = ""

    def _ev(self, kind: str, **kw) -> None:
        try:
            self.emit({"type": "term", "run": self.run, "kind": kind, **kw})
        except Exception:      # noqa: BLE001 — a display callback never fails the run
            pass

    def feed(self, line: str) -> None:
        try:
            d = json.loads(line)
        except ValueError:
            return
        t = d.get("type")
        if t == "system" and d.get("subtype") == "init":
            self.session_id = d.get("session_id", "") or self.session_id
        elif t == "system" and d.get("subtype") == "permission_denied":
            msg = str(d.get("message") or d.get("decision_reason") or "refused")
            self.denials.append({"tool": d.get("tool_name", ""), "message": msg})
            self._ev("denied", id=d.get("tool_use_id", ""), tool=d.get("tool_name", ""),
                     text=_redact("term", msg)[:400])
        elif t == "stream_event":
            ev = d.get("event") or {}
            if ev.get("type") == "message_start":
                self._cur_msg = (ev.get("message") or {}).get("id", "")
            elif ev.get("type") == "content_block_start":
                self._block = ""
            elif ev.get("type") == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    self._delta_msgs.add(self._cur_msg)
                    first = not self._block
                    self._block += delta["text"]
                    self._ev("text", text=_redact("term", self._block), new=first)
        elif t == "assistant":
            msg = d.get("message") or {}
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    name, inp = b.get("name", ""), b.get("input") or {}
                    self.tools[b.get("id", "")] = name
                    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if fp:
                            self.touched.add(str(fp))
                    self._block = ""
                    self._ev("tool", id=b.get("id", ""), name=name,
                             summary=_redact(name, tool_summary(name, inp, self.real)))
                elif b.get("type") == "text" and msg.get("id") not in self._delta_msgs:
                    # partial messages off, or a block that never streamed
                    if b.get("text"):
                        self._ev("text", text=_redact("term", b["text"]), whole=True)
        elif t == "user":
            content = (d.get("message") or {}).get("content")
            if not isinstance(content, list):
                return
            tur = d.get("tool_use_result")
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                tid = b.get("tool_use_id", "")
                name = self.tools.get(tid, "")
                c = b.get("content")
                if isinstance(c, list):
                    c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                ev = {"id": tid, "name": name, "ok": not b.get("is_error"),
                      "text": _redact(name or "term", str(c or ""))}
                if isinstance(tur, dict) and tur.get("type") == "create" \
                        and isinstance(tur.get("content"), str):
                    # a new file: the patch is empty and the content comes whole
                    body = tur["content"].splitlines()
                    ev["added"], ev["removed"] = len(body), 0
                    ev["diff"] = _redact(name, "\n".join("+" + ln for ln in body[:200]))
                elif isinstance(tur, dict) and isinstance(tur.get("structuredPatch"), list):
                    plus = minus = 0
                    diff = []
                    for h in tur["structuredPatch"]:
                        for ln in (h.get("lines") or []):
                            if ln.startswith("+"):
                                plus += 1
                            elif ln.startswith("-"):
                                minus += 1
                            diff.append(ln)
                    ev["added"], ev["removed"] = plus, minus
                    ev["diff"] = _redact(name, "\n".join(diff[:200]))
                self._ev("result", **ev)
        elif t == "result":
            self.result = d
            self.session_id = d.get("session_id", "") or self.session_id


# ── the tool ────────────────────────────────────────────────────────────────

SCHEMA = {
    "name": "code_task",
    "description": (
        "Work on an existing code project with Claude Code: it reads the project "
        "(including its CLAUDE.md), edits files inside that folder and runs its "
        "tests. Use for changes to a real codebase the user names by folder. "
        "Continues the previous session for that folder unless new_session is "
        "true. The user approves every run and can watch it live."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_dir": {"type": "string",
                            "description": "Absolute path of the project folder"},
            "task": {"type": "string",
                     "description": "What to do, in full; Claude Code sees only this"},
            "new_session": {"type": "boolean", "default": False,
                            "description": "Start fresh instead of continuing this folder's last session"},
            "resume_latest": {"type": "boolean", "default": False,
                              "description": "Admin only: continue the most recent Claude Code "
                                             "session in this folder, including ones started outside SUNI"},
        },
        "required": ["project_dir", "task"],
    },
}


async def handler(project_dir: str, task: str, new_session: bool = False,
                  resume_latest: bool = False) -> str:
    from .claude_code_advanced import EVENT_CB_CTX, _run_claude_stream
    from .agent_tool import CURRENT_ROLE

    role = CURRENT_ROLE.get("standard")
    if role in _NEVER_ROLES:
        return f"Coding work in project folders is not available to the {role} role."
    if resume_latest and role != "admin":
        return ("Only an admin can continue a Claude Code session started outside "
                "SUNI: it may belong to someone else's work in that folder.")

    event_cb = EVENT_CB_CTX.get()
    if event_cb is None:
        # Every run is approved by a person; with nobody connected to approve,
        # say so now instead of waiting out the approval timeout.
        return ("This needs a live session where you can approve it (the web chat "
                "or the face page). It can't run from here.")

    real, why = resolve_project(project_dir)
    if not real:
        return why

    key = os.path.normcase(real)
    lock = _locks.setdefault(key, asyncio.Lock())
    if lock.locked():
        return f"Claude Code is already working in {real}. Wait for that run to finish or stop it."

    user_id = USER_ID_CTX.get("")
    async with lock:
        run_id = hashlib.sha1(f"{key}{time.time()}".encode()).hexdigest()[:10]
        sid = "" if (new_session or resume_latest) else last_session(user_id, real)

        args = ["--print", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages",
                "--permission-mode", "acceptEdits",
                "--setting-sources", "project",
                "--strict-mcp-config",
                "--allowedTools", allowed_tools_arg()]
        if resume_latest:
            args += ["--continue"]
        elif sid:
            args += ["--resume", sid]

        parser = StreamParser(event_cb, real, run_id)
        parser._ev("start", project=real, agent="claude",
                   session=("latest" if resume_latest else sid or "new"),
                   commands=[c.replace(":*", "") for c in _commands()])

        from .. import usage as _usage
        _usage.record_model("claude-code (CLI, model chosen by the CLI)")

        before = await asyncio.to_thread(snapshot, real)
        timeout = int(_cfg.get("code_task_timeout", 1800) or 1800)
        t0 = time.monotonic()
        try:
            rc, stdout, stderr = await _run_claude_stream(
                args, parser.feed, timeout=timeout, cwd=real, stdin_data=task)
        except asyncio.CancelledError:
            # shielded: the request is being cancelled, but what the run had
            # already changed is worth one last look
            try:
                ch = await asyncio.shield(asyncio.to_thread(
                    changes_since, real, before, parser.touched))
            except BaseException:      # noqa: BLE001
                ch = []
            parser._ev("end", ok=False, stopped=True, changes=ch)
            raise

        changes = await asyncio.to_thread(changes_since, real, before, parser.touched)
        res = parser.result
        if res:
            try:
                from ..models.claude_code_agent import _record_cc_usage
                _record_cc_usage(res)
            except Exception:      # noqa: BLE001
                pass
        if parser.session_id:
            _save_session(user_id, real, parser.session_id)

        ok = rc == 0 and bool(res) and not res.get("is_error")
        elapsed = round(time.monotonic() - t0)
        parser._ev("end", ok=ok, seconds=elapsed,
                   cost=res.get("total_cost_usd"), turns=res.get("num_turns"),
                   session=parser.session_id, changes=changes,
                   denied=len(parser.denials),
                   error=("" if ok else _redact("term", (stderr or "").strip())[:600]))

        lines = []
        if ok:
            lines.append(f"Claude Code finished in {real} after {elapsed}s.")
        elif "timed out" in (stderr or ""):
            lines.append(f"Claude Code was stopped after the {timeout}s limit in {real}.")
        else:
            lines.append(f"Claude Code did not finish cleanly in {real}: "
                         f"{(stderr or '').strip()[:300] or 'no result'}")
        if isinstance(res.get("result"), str) and res["result"].strip():
            lines += ["", res["result"].strip()]
        if changes:
            lines += ["", "Files changed by this run:"]
            for c in changes[:40]:
                n = f" (+{c['added']} -{c['removed']})" if "added" in c else ""
                note = " — it already had uncommitted changes before" if c.get("was_dirty") else ""
                lines.append(f"  {c['status']}: {c['path']}{n}{note}")
        else:
            lines += ["", "No files were changed."]
        if parser.denials:
            lines += ["", "Refused by its permissions (not worked around):"]
            for dn in parser.denials[:10]:
                lines.append(f"  {dn['tool']}: {dn['message'][:160]}")
        if parser.session_id:
            lines += ["", f"[code session: {parser.session_id}]"]
        return "\n".join(lines)
