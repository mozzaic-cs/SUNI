"""CodexAgent — wraps the OpenAI Codex CLI (`codex exec`) as a BaseAgent.

A second no-key, subscription-authenticated frontier provider alongside Claude
Code — the two decorrelated models the multi-model "orchestrate" mode collaborates
between. Auth comes from ~/.codex/auth.json (ChatGPT subscription); an optional
OPENAI_API_KEY override supports API-key usage too.

Windows-safe subprocess handling mirrors the Claude Code agent: the prompt is
delivered on a REAL FILE HANDLE as stdin (not argv, not an asyncio pipe), and the
final answer is read from Codex's `-o/--output-last-message` file rather than
parsed out of the agentic event stream.
"""
from __future__ import annotations
import os
import glob
import shutil
import asyncio
import tempfile
import logging
from ..core.base_agent import BaseAgent
from ..core.message import Message, Role
from ..core.context import Context

log = logging.getLogger("suni.codex")

_CODEX_HOME = os.path.expanduser("~")


def _codex_cmd() -> str | None:
    """Resolve the codex executable. PATH first (refreshed session / shim), then
    the versioned install under %LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<hash>\\codex.exe
    (newest), then an explicit config override. The <hash> changes across versions
    so it is discovered, never hardcoded."""
    from .. import config as _cfg
    override = str(_cfg.get("codex_cmd_path", "") or "").strip()
    if override and os.path.exists(override):
        return override
    for name in ("codex.exe", "codex.cmd", "codex"):
        found = shutil.which(name)
        if found:
            return found

    # Windows: the versioned install under %LOCALAPPDATA%. Guarded on the
    # variable being set — unset (i.e. every POSIX box) it produced a RELATIVE
    # glob of "OpenAI/Codex/bin/*" against the current directory, which is
    # meaningless at best and a surprise match at worst.
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        cands = glob.glob(os.path.join(local_app, "OpenAI", "Codex", "bin", "*", "codex.exe"))
        if cands:
            return max(cands, key=os.path.getmtime)   # newest install

    # POSIX: common install locations that are not always on a service's PATH
    # (systemd units get a minimal PATH, so a working shell install can still
    # be invisible here).
    for cand in (
        os.path.expanduser("~/.local/bin/codex"),
        os.path.expanduser("~/.npm-global/bin/codex"),
        "/usr/local/bin/codex",
        "/opt/homebrew/bin/codex",
    ):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


async def _run_codex(prompt: str, timeout: int = 300, cwd: str | None = None,
                     model: str = "", api_key: str = "") -> tuple[int, str, str]:
    """Run `codex exec` non-interactively and return (rc, final_message, stderr).

    read-only sandbox + ephemeral session + skip-git-repo-check keep it side-effect
    free and runnable anywhere. The final agent message is captured via -o."""
    cmd = _codex_cmd()
    if not cmd:
        return 1, "", ("Codex CLI not found. Install/login with the OpenAI Codex app, "
                       "or set codex_cmd_path in config.")

    env = os.environ.copy()
    if api_key:                       # optional API-key mode; subscription needs no key
        env["OPENAI_API_KEY"] = api_key

    args = ["exec", "--skip-git-repo-check", "--ephemeral",
            "-s", "read-only", "--color", "never"]
    if model:
        args += ["-m", model]

    # Prompt via a real file handle on stdin (`-` = read prompt from stdin).
    _fd, _in_path = tempfile.mkstemp(suffix=".txt", prefix="suni_codex_in_")
    with os.fdopen(_fd, "w", encoding="utf-8") as _f:
        _f.write(prompt)
    _out_fd, _out_path = tempfile.mkstemp(suffix=".txt", prefix="suni_codex_out_")
    os.close(_out_fd)
    args += ["-o", _out_path, "-"]

    _stdin = open(_in_path, "rb")
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd, *args,
            stdin=_stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd or _CODEX_HOME,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 1, "", f"Codex timed out after {timeout}s"
        # Prefer the clean final-message file; fall back to stdout.
        final = ""
        try:
            final = open(_out_path, "r", encoding="utf-8", errors="replace").read().strip()
        except OSError:
            pass
        if not final:
            final = stdout.decode("utf-8", errors="replace").strip()
        return proc.returncode, final, stderr.decode("utf-8", errors="replace")
    finally:
        _stdin.close()
        for _p in (_in_path, _out_path):
            try:
                os.unlink(_p)
            except OSError:
                pass


class CodexAgent(BaseAgent):
    def __init__(self, name: str = "codex", model: str = "", api_key: str = ""):
        super().__init__(name)
        self.model = model
        self._api_key = api_key

    async def chat(
        self,
        messages: list[Message],
        context: Context,
        tools: list[dict] | None = None,
    ) -> Message:
        user_msgs = [m for m in messages if m.role == Role.USER]
        if not user_msgs:
            return Message(role=Role.ASSISTANT, content="No task provided.", agent=self.name)
        task = user_msgs[-1].content

        # Fold in orchestrator-built context (memory/lang) + a couple of prior turns.
        prefix = []
        for m in messages:
            if m.role == Role.SYSTEM and m.agent in ("memory", "lang") and m.content:
                prefix.append(m.content)
        prior = [m for m in messages if m.role in (Role.USER, Role.ASSISTANT)][:-1]
        if prior:
            prefix.append("[Prior conversation]\n" + "\n".join(
                f"{'User' if m.role == Role.USER else 'Assistant'}: {m.content[:300]}"
                for m in prior[-4:]))
        if prefix:
            task = "\n\n".join(prefix) + "\n\n" + task

        from .. import config as _cfg
        timeout = int(_cfg.get("codex_timeout", 300) or 300)
        rc, final, stderr = await _run_codex(
            task, timeout=timeout, model=self.model, api_key=self._api_key)

        if rc != 0 and not final.strip():
            content = f"Codex returned an error (exit {rc}): {stderr.strip() or 'no output'}"
        else:
            content = final
        return Message(role=Role.ASSISTANT, content=content, agent=self.name)
