"""
Advanced Claude Code CLI tools — JSON output, session continuity, subagents, scheduling.

Per-user API key
----------------
Set CLAUDE_API_KEY_CTX (a contextvars.ContextVar) before executing any tool in this
module to override the ANTHROPIC_API_KEY environment variable for that subprocess.
The orchestrator sets this from the resolved per-user key each request.
"""
from __future__ import annotations
import asyncio
import json
import os
import shutil
import tempfile
from contextvars import ContextVar
from typing import Any

# ContextVar: set by orchestrator to the resolved per-user API key (or '' for global)
CLAUDE_API_KEY_CTX: ContextVar[str] = ContextVar("claude_api_key", default="")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _claude_cmd() -> str | None:
    for name in ("claude.cmd", "claude.CMD", "claude"):
        found = shutil.which(name)
        if found:
            return found
    return None


async def _run_claude(args: list[str], timeout: int = 300, cwd: str | None = None,
                      stdin_data: str | None = None) -> tuple[int, str, str]:
    cmd = _claude_cmd()
    if not cmd:
        return 1, "", "Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code"

    # Build subprocess environment: inherit current env, then apply per-user key override
    env = os.environ.copy()
    user_key = CLAUDE_API_KEY_CTX.get("")
    if user_key:
        env["ANTHROPIC_API_KEY"] = user_key

    # The prompt is delivered on stdin via a REAL FILE HANDLE — not argv, not an
    # asyncio pipe. Two Windows traps this avoids:
    #  1. argv would overflow cmd.exe's ~8191-char command line for large prompts
    #     ("The command line is too long"), since `claude.cmd` re-forwards args.
    #  2. an asyncio stdin PIPE + communicate(input=...) does NOT deliver stdin
    #     through the .cmd shim on the ProactorEventLoop — Claude Code warns
    #     "no stdin data received in 3s" and runs with an empty prompt.
    # A file fd is inherited by the child and read directly — reliable for both
    # small and large prompts, and it preserves newlines.
    _tmp_path = None
    _stdin = None
    if stdin_data is not None:
        _fd, _tmp_path = tempfile.mkstemp(suffix=".txt", prefix="suni_cc_")
        with os.fdopen(_fd, "w", encoding="utf-8") as _f:
            _f.write(stdin_data)
        _stdin = open(_tmp_path, "rb")
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd, *args,
            stdin=_stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            proc.kill()
            return 1, "", f"Claude Code timed out after {timeout}s"
    finally:
        if _stdin is not None:
            _stdin.close()
        if _tmp_path is not None:
            try:
                os.unlink(_tmp_path)
            except OSError:
                pass


def _parse_json_output(raw: str) -> dict[str, Any]:
    """Extract JSON from claude --output-format json output."""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"result": raw.strip()}


# ---------------------------------------------------------------------------
# Schemas + handlers
# ---------------------------------------------------------------------------

TASK_SCHEMA = {
    "name": "claude_task",
    "description": (
        "Run a task with Claude Code CLI and get a structured JSON response. "
        "Returns the result text and a session_id for follow-up continuity. "
        "Use for complex coding tasks, analysis, or multi-step work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The task or question for Claude Code to complete"},
            "allowed_tools": {
                "type": "string",
                "description": "Comma-separated list of tools to allow, e.g. 'Bash,Read,Write'. Omit for all tools.",
                "default": ""
            },
            "session_id": {
                "type": "string",
                "description": "Resume a previous Claude Code session by ID (from prior claude_task result).",
                "default": ""
            },
            "system_prompt": {
                "type": "string",
                "description": "Optional additional system context to inject for this task.",
                "default": ""
            },
            "timeout": {"type": "integer", "description": "Max seconds to wait (default 300)", "default": 300},
        },
        "required": ["task"],
    },
}


async def task_handler(
    task: str,
    allowed_tools: str = "",
    session_id: str = "",
    system_prompt: str = "",
    timeout: int = 300,
) -> str:
    args = ["--print", "--output-format", "json"]
    if allowed_tools:
        args += ["--allowedTools", allowed_tools]
    if session_id:
        args += ["--resume", session_id]
    if system_prompt:
        args += ["--system-prompt", system_prompt]

    rc, stdout, stderr = await _run_claude(args, timeout=timeout, stdin_data=task)
    if rc != 0 and not stdout.strip():
        return f"Error (exit {rc}): {stderr.strip() or 'no output'}"

    parsed = _parse_json_output(stdout)
    result = parsed.get("result", parsed.get("content", stdout.strip()))
    sid = parsed.get("session_id", "")

    response = result
    if sid:
        response += f"\n\n[session_id: {sid}]"
    return response


# ---------------------------------------------------------------------------

AGENT_SCHEMA = {
    "name": "claude_create_agents",
    "description": (
        "Spawn one or more named Claude Code sub-agents with custom system prompts. "
        "Each agent runs independently and their results are returned together. "
        "Useful for parallelising complex tasks — e.g. one agent analyses, another writes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The overall task these agents should accomplish"},
            "agents": {
                "type": "array",
                "description": "List of agent definitions",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "prompt": {"type": "string"},
                    },
                    "required": ["name", "prompt"],
                },
            },
            "timeout": {"type": "integer", "default": 300},
        },
        "required": ["task", "agents"],
    },
}


async def agent_handler(task: str, agents: list[dict], timeout: int = 300) -> str:
    agents_json = json.dumps({a["name"]: {"prompt": a["prompt"]} for a in agents})
    args = ["--print", "--agents", agents_json, "--output-format", "json"]
    rc, stdout, stderr = await _run_claude(args, timeout=timeout, stdin_data=task)
    if rc != 0 and not stdout.strip():
        return f"Error (exit {rc}): {stderr.strip() or 'no output'}"
    parsed = _parse_json_output(stdout)
    return parsed.get("result", stdout.strip())


# ---------------------------------------------------------------------------

INIT_SCHEMA = {
    "name": "claude_init_project",
    "description": (
        "Initialise a project directory with CLAUDE.md using Claude Code's /init equivalent. "
        "Analyses the codebase and generates project documentation and conventions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Absolute path to the project directory"},
            "timeout": {"type": "integer", "default": 120},
        },
        "required": ["directory"],
    },
}


async def init_handler(directory: str, timeout: int = 120) -> str:
    task = (
        f"Analyse the project at {directory} and create or update a CLAUDE.md file "
        "documenting: project purpose, key files, architecture, coding conventions, "
        "and any important context a developer needs. Be concise and practical."
    )
    args = ["--print", "--allowedTools", "Read,Write,Glob,Grep", "--output-format", "json"]
    rc, stdout, stderr = await _run_claude(args, timeout=timeout, stdin_data=task)
    if rc != 0 and not stdout.strip():
        return f"Error (exit {rc}): {stderr.strip()}"
    parsed = _parse_json_output(stdout)
    return parsed.get("result", stdout.strip())


# ---------------------------------------------------------------------------

SCHEDULE_SCHEMA = {
    "name": "claude_schedule_task",
    "description": (
        "Ask Claude Code to create a Windows scheduled task or recurring job. "
        "Claude Code will use schtasks or Task Scheduler XML to set it up."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "What the scheduled task should do"},
            "schedule": {"type": "string", "description": "When to run, e.g. 'daily at 9am', 'every Monday at 6am', 'on logon'"},
            "command": {"type": "string", "description": "The command or script to execute"},
            "task_name": {"type": "string", "description": "Windows Task Scheduler task name"},
            "timeout": {"type": "integer", "default": 120},
        },
        "required": ["description", "schedule", "command", "task_name"],
    },
}


async def schedule_handler(
    description: str, schedule: str, command: str, task_name: str, timeout: int = 120
) -> str:
    task = f"""Create a Windows scheduled task with these exact details:
- Task name: {task_name}
- Command: {command}
- Schedule: {schedule}
- Purpose: {description}

IMPORTANT — Windows path rules:
1. Use PowerShell (not bash) to run schtasks, to avoid Git Bash path mangling.
2. Run this PowerShell command:
   powershell -Command "schtasks /create /tn '{task_name}' /tr '{command}' /sc <SCHEDULE_TYPE> /st <HH:MM> /f"
   Fill in /sc (DAILY/WEEKLY/MONTHLY/ONCE/ONLOGON) and /st from the schedule description.
3. Then verify with: powershell -Command "schtasks /query /tn '{task_name}'"
4. Report success or any error output.
"""
    args = ["--print", "--allowedTools", "Bash", "--output-format", "json"]
    rc, stdout, stderr = await _run_claude(args, timeout=timeout, stdin_data=task)
    if rc != 0 and not stdout.strip():
        return f"Error (exit {rc}): {stderr.strip()}"
    parsed = _parse_json_output(stdout)
    return parsed.get("result", stdout.strip())


# ---------------------------------------------------------------------------

ADVISOR_SCHEMA = {
    "name": "claude_advisor",
    "description": (
        "Get strategic advice from Claude Code on architecture, approach, or code quality "
        "without making any changes. Claude Code reads the codebase and returns recommendations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The architectural or design question to advise on"},
            "context_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "File or directory paths Claude Code should read for context",
                "default": [],
            },
            "timeout": {"type": "integer", "default": 180},
        },
        "required": ["question"],
    },
}


async def advisor_handler(question: str, context_paths: list[str] | None = None, timeout: int = 180) -> str:
    paths_hint = ""
    if context_paths:
        paths_hint = f"\nConsult these paths for context: {', '.join(context_paths)}"
    task = f"Act as a senior software architect. Answer this question with concrete recommendations:{paths_hint}\n\n{question}"
    args = ["--print", "--allowedTools", "Read,Glob,Grep", "--output-format", "json"]
    rc, stdout, stderr = await _run_claude(args, timeout=timeout, stdin_data=task)
    if rc != 0 and not stdout.strip():
        return f"Error (exit {rc}): {stderr.strip()}"
    parsed = _parse_json_output(stdout)
    return parsed.get("result", stdout.strip())
