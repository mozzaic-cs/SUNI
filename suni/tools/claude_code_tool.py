"""
Claude Code CLI tool — invokes `claude -p "<task>"` as a subprocess.

Suni uses this to delegate complex coding, file editing, analysis,
or multi-step dev tasks to Claude Code, which can read/write files
and run shell commands autonomously.
"""
from __future__ import annotations
import asyncio
import os
import shutil
import tempfile

SCHEMA = {
    "name": "claude_code",
    "description": (
        "Invoke Claude Code CLI for complex software tasks: writing or editing code, "
        "analyzing a codebase, debugging, refactoring, explaining code, running tests, "
        "or any multi-step development task that requires reading/writing files. "
        "Claude Code can autonomously browse files and execute shell commands."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Full description of the task for Claude Code to perform",
            },
            "working_dir": {
                "type": "string",
                "description": "Working directory for the task (defaults to current dir)",
            },
            "model": {
                "type": "string",
                "description": "Claude model to use (e.g. claude-sonnet-4-6). Optional.",
            },
        },
        "required": ["task"],
    },
}


def _claude_cmd() -> list[str] | None:
    """Locate the claude executable, handling Windows .cmd wrappers."""
    # Prefer the .cmd wrapper on Windows so the shell resolves it correctly
    for suffix in (".cmd", ".CMD", ""):
        path = shutil.which(f"claude{suffix}")
        if path:
            return [path]
    return None


async def handler(
    task: str,
    working_dir: str = ".",
    model: str | None = None,
    timeout: int = 180,
) -> str:
    cmd_base = _claude_cmd()
    if not cmd_base:
        return (
            "Error: 'claude' CLI not found in PATH. "
            "Install it with: npm install -g @anthropic-ai/claude-code"
        )

    # Prompt goes via stdin through a REAL FILE HANDLE (not argv, not an asyncio
    # pipe). argv overflows cmd.exe's ~8191-char limit for large tasks; an
    # asyncio stdin PIPE fails to reach Claude Code through the .cmd shim on
    # Windows' ProactorEventLoop ("no stdin data received in 3s"). A file fd the
    # child reads directly is reliable for both.
    cmd = cmd_base + ["--print"]
    if model:
        cmd += ["--model", model]

    _fd, _tmp_path = tempfile.mkstemp(suffix=".txt", prefix="suni_cc_")
    with os.fdopen(_fd, "w", encoding="utf-8") as _f:
        _f.write(task)
    _stdin = open(_tmp_path, "rb")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=_stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()

        if proc.returncode != 0 and not output:
            return f"Claude Code error (exit {proc.returncode}):\n{err}"

        return output or "(Claude Code returned no output)"

    except asyncio.TimeoutError:
        return f"Error: Claude Code timed out after {timeout}s"
    except Exception as e:
        return f"Error invoking Claude Code: {e}"
    finally:
        _stdin.close()
        try:
            os.unlink(_tmp_path)
        except OSError:
            pass
