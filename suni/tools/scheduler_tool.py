"""
Native Windows Task Scheduler tool — creates scheduled tasks via subprocess.
No Claude Code or elevation required; runs in the current user context.
"""
from __future__ import annotations
import subprocess
import re

SCHEMA = {
    "name": "create_scheduled_task",
    "description": (
        "Create a scheduled task that runs a command on a schedule. "
        "Use this for any recurring job: email digests, backups, scripts, etc. "
        "Runs in the current user context — no elevation required. "
        "WINDOWS ONLY: this uses Task Scheduler (schtasks). On Linux or macOS "
        "it reports that it is unavailable — use cron or a systemd timer there."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_name": {
                "type": "string",
                "description": "Unique task name in Task Scheduler, e.g. 'SUNI_EmailDigest'",
            },
            "command": {
                "type": "string",
                "description": "Full command to run, including the interpreter "
                               "and the absolute path to the script",
            },
            "schedule": {
                "type": "string",
                "description": "Schedule type: DAILY, WEEKLY, MONTHLY, ONCE, ONLOGON, ONSTART",
            },
            "time": {
                "type": "string",
                "description": "Start time in HH:MM 24h format, e.g. '08:00'. Required for DAILY/WEEKLY/MONTHLY.",
                "default": "",
            },
            "days": {
                "type": "string",
                "description": "For WEEKLY: day(s) e.g. 'MON', 'MON,WED,FRI'. For MONTHLY: day number e.g. '1'.",
                "default": "",
            },
        },
        "required": ["task_name", "command", "schedule"],
    },
}


def _parse_schedule(schedule: str) -> str:
    s = schedule.upper().strip()
    if s in ("DAILY", "WEEKLY", "MONTHLY", "ONCE", "ONLOGON", "ONSTART", "ONIDLE"):
        return s
    if "DAY" in s:
        return "DAILY"
    if "WEEK" in s:
        return "WEEKLY"
    if "MONTH" in s:
        return "MONTHLY"
    if "LOGON" in s or "LOGIN" in s:
        return "ONLOGON"
    if "BOOT" in s or "START" in s:
        return "ONSTART"
    return "DAILY"


async def handler(
    task_name: str,
    command: str,
    schedule: str,
    time: str = "",
    days: str = "",
) -> str:
    sc = _parse_schedule(schedule)

    args = [
        "schtasks", "/create",
        "/tn", task_name,
        "/tr", command,
        "/sc", sc,
        "/f",   # overwrite if exists
    ]

    if time and sc in ("DAILY", "WEEKLY", "MONTHLY", "ONCE"):
        # Validate HH:MM
        if not re.match(r"^\d{1,2}:\d{2}$", time):
            return f"Invalid time format '{time}' — use HH:MM (e.g. '08:00')"
        args += ["/st", time]

    if days and sc == "WEEKLY":
        args += ["/d", days.upper()]
    elif days and sc == "MONTHLY":
        args += ["/d", days]

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            out = result.stdout.strip() or "Task created successfully."
            return f"{out}\nTask '{task_name}' scheduled ({sc}{' at ' + time if time else ''})."
        else:
            err = result.stderr.strip() or result.stdout.strip()
            return f"Failed to create task (exit {result.returncode}): {err}"
    except FileNotFoundError:
        return (
            "Scheduled tasks are not available on this platform. "
            "create_scheduled_task uses Windows Task Scheduler (schtasks); "
            "on Linux use cron or a systemd timer, on macOS use launchd."
        )
    except subprocess.TimeoutExpired:
        return "schtasks timed out."
    except Exception as e:
        return f"Error: {e}"
