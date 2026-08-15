"""Orchestrator tools for the background task queue."""
from __future__ import annotations
from .. import task_queue as _tq

LIST_SCHEMA = {
    "name": "tasks_list",
    "description": "List background tasks (pending, running, done, failed). Shows recent tasks for the current user.",
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Filter by status: pending, running, done, failed, cancelled, or all (default)",
                "default": "all",
            },
        },
        "required": [],
    },
}

STATUS_SCHEMA = {
    "name": "task_status",
    "description": "Get the current status and result of a specific background task by ID.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID (from tasks_list)"},
        },
        "required": ["task_id"],
    },
}


def list_handler(status: str = "all") -> str:
    tasks = _tq.list_tasks()
    if status != "all":
        tasks = [t for t in tasks if t["status"] == status]
    if not tasks:
        return f"No {'background' if status == 'all' else status} tasks."
    lines = []
    for t in tasks:
        ts = t.get("created_at", "")[:16].replace("T", " ")
        prog = t.get("progress", "")
        prog_str = f" [{prog}%]" if prog.isdigit() else (f" [{prog}]" if prog else "")
        lines.append(f"[{t['id']}] {t['status'].upper():10} {t['title']}{prog_str} — {ts}")
        if t.get("result") and t["status"] == "done":
            lines.append(f"  → {str(t['result'])[:100]}")
        if t.get("error") and t["status"] == "failed":
            lines.append(f"  ✗ {str(t['error'])[:100]}")
    return "\n".join(lines)


def status_handler(task_id: str) -> str:
    task = _tq.get_task(task_id)
    if not task:
        return f"Task '{task_id}' not found."
    lines = [
        f"Task: {task['title']} [{task['id']}]",
        f"Status: {task['status']}",
        f"Progress: {task.get('progress', 'n/a')}",
    ]
    if task.get("started_at"):
        lines.append(f"Started: {task['started_at'][:19].replace('T', ' ')}")
    if task.get("completed_at"):
        lines.append(f"Completed: {task['completed_at'][:19].replace('T', ' ')}")
    if task.get("result"):
        lines.append(f"Result: {str(task['result'])[:400]}")
    if task.get("error"):
        lines.append(f"Error: {task['error']}")
    return "\n".join(lines)
