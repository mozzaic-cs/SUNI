from __future__ import annotations
import asyncio
import re
import shlex

SCHEMA = {
    "name": "run_shell",
    "description": "Execute a shell command and return stdout/stderr output.",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
        },
        "required": ["command"],
    },
}

# ── Destructive-command guard ────────────────────────────────────────────────
# A heuristic speed bump, NOT a sandbox: it refuses commands that are
# irreversible by shape. Anything genuinely privileged should go through
# claude_task, which is gated by the approval flow.
#
# Both the Windows and POSIX sets are always applied, deliberately. Selecting
# by platform would mean a detection bug silently disables the guard, and
# blocking `mkfs` on Windows costs nothing.
#
# Each entry is (pattern, reason) so a refusal can say WHICH rule matched —
# an opaque "blocked" teaches the model nothing and looks like a bug to users.
#
# Boundaries are per-pattern on purpose. The previous single \b(...)\b wrapper
# silently broke every alternative ending in a non-word character: `format c:`
# ends in ':', so no word boundary could follow it and the pattern never
# matched the very command it was written to stop.
_DENY_RULES: list[tuple[str, str]] = [
    # ── Windows ──
    (r'\bformat\s+[a-zA-Z]:',                    "formats a drive"),
    (r'\b(rd|rmdir)\s+/s',                       "recursive directory delete"),
    (r'\bdel\s+/[sfq]',                          "forced/recursive file delete"),
    (r'Remove-Item\b[^|;&]*-Recurse',            "recursive delete (PowerShell)"),
    (r'\breg\s+(delete|add)\s+HKLM',             "modifies machine-wide registry"),
    (r'\b(bcdedit|diskpart)\b',                  "alters boot config or partitions"),
    (r'\bcipher\s+/w',                           "wipes free space"),
    (r'\bvssadmin\s+delete\s+shadows',           "deletes volume shadow copies"),
    (r'\bwmic\s+shadowcopy\s+delete',            "deletes volume shadow copies"),
    (r'\bnet\s+(user|localgroup)\b',             "modifies local accounts"),
    (r'\btaskkill\s+/f\s+/im\s+python',          "kills SUNI's own process"),
    (r'\b(Stop|Restart)-Computer\b',             "shuts down or reboots the machine"),
    # ── POSIX ──
    (r'\brm\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*r',     "recursive delete"),
    (r'\bmkfs(\.[a-z0-9]+)?\b',                  "creates a filesystem (destroys data)"),
    (r'\bdd\b[^|;&]*\bof=/dev/',                 "writes directly to a block device"),
    (r'>\s*/dev/(sd|nvme|hd|disk|vd)',           "redirects output onto a raw disk"),
    (r':\s*\(\s*\)\s*\{.*\|.*&.*\}\s*;?\s*:',    "fork bomb"),
    (r'\bch(mod|own)\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*R[a-zA-Z]*\s+[^\s]+\s+/\s*$',
                                                 "recursive permission change on /"),
    (r'\bshred\b',                               "irreversibly overwrites files"),
    (r'\bcrontab\s+-r',                          "deletes all scheduled jobs"),
    (r'(?:^|[;&|]\s*)(userdel|usermod|passwd)\s', "modifies user accounts"),
    (r'\biptables\s+-F|\bufw\s+disable',         "flushes firewall rules"),
    (r'\bsystemctl\s+(stop|disable|mask)\b',     "stops or disables system services"),
    (r'\bkill\s+-9\s+1\b|\bkillall\b',           "kills init or mass-kills processes"),
    (r'\b(halt|poweroff|reboot)\b|\binit\s+[06]\b', "shuts down or reboots the machine"),
    (r'\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|k)?sh',
                                                 "pipes a downloaded script straight into a shell"),
    # ── Both ──
    (r'\bshutdown\b',                            "shuts down the machine"),
]
_DENYLIST: list[tuple[re.Pattern, str]] = [
    (re.compile(p, re.IGNORECASE), why) for p, why in _DENY_RULES
]


def _blocked_reason(command: str) -> str | None:
    """Return why a command is refused, or None when it is allowed."""
    for pattern, why in _DENYLIST:
        if pattern.search(command):
            return why
    return None

# Hard cap on output to avoid OOM from runaway commands
_MAX_OUTPUT_BYTES = 512 * 1024   # 512 KB


async def handler(command: str, timeout: int = 30) -> str:
    reason = _blocked_reason(command)
    if reason:
        return (f"Error: command blocked — {reason}. "
                "Use claude_task to perform system changes that require elevated care.")
    try:
        # Use create_subprocess_shell so PowerShell pipelines and builtins still work,
        # but cap output and enforce timeout to prevent resource exhaustion.
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"Error: command timed out after {timeout}s"

        out = (stdout or b"")[:_MAX_OUTPUT_BYTES].decode(errors="replace")
        err = (stderr or b"")[:_MAX_OUTPUT_BYTES].decode(errors="replace")
        if len(stdout or b"") > _MAX_OUTPUT_BYTES:
            out += "\n[output truncated at 512 KB]"

        result = out
        if err:
            result += f"\n[stderr]\n{err}"
        return result.strip() or "(no output)"
    except Exception as e:
        return f"Error: {e}"
