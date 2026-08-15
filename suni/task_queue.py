"""
Background task queue for SUNI.

Allows users to queue long-running tasks that run independently of the
chat session. Progress is streamed via SSE; push notification is sent
on completion.

Storage: memory/bg_tasks.db (SQLite, WAL mode)

Statuses: pending → running → done | failed | cancelled
"""
from __future__ import annotations
import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Awaitable

log = logging.getLogger("suni.task_queue")

_DB = Path("memory/bg_tasks.db")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bg_tasks (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    user_id      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    started_at   TEXT DEFAULT NULL,
    completed_at TEXT DEFAULT NULL,
    result       TEXT DEFAULT NULL,
    error        TEXT DEFAULT NULL,
    progress     TEXT NOT NULL DEFAULT '0',   -- "0"–"100" or human text
    notify_channel TEXT DEFAULT ''            -- 'telegram' | 'whatsapp' | ''
);
CREATE INDEX IF NOT EXISTS idx_bgt_user   ON bg_tasks(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bgt_status ON bg_tasks(status);
"""


def _conn() -> sqlite3.Connection:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA_SQL)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── CRUD ─────────────────────────────────────────────────────────────────────

def create_task(
    title: str,
    description: str = "",
    user_id: str = "",
    notify_channel: str = "",
) -> dict:
    tid = str(uuid.uuid4())[:10]
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO bg_tasks (id, title, description, status, user_id, created_at, notify_channel) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
            (tid, title, description, user_id, now, notify_channel),
        )
    return get_task(tid)


def get_task(task_id: str) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT * FROM bg_tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_tasks(user_id: str = "", limit: int = 50) -> list[dict]:
    conn = _conn()
    if user_id:
        rows = conn.execute(
            "SELECT * FROM bg_tasks WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM bg_tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _update(task_id: str, **fields) -> None:
    if not fields:
        return
    sets  = ", ".join(f"{k} = ?" for k in fields)
    vals  = list(fields.values()) + [task_id]
    with _conn() as conn:
        conn.execute(f"UPDATE bg_tasks SET {sets} WHERE id = ?", vals)


def cancel_task(task_id: str, user_id: str) -> bool:
    conn = _conn()
    row = conn.execute("SELECT status, user_id FROM bg_tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    if not row or row["user_id"] != user_id:
        return False
    if row["status"] in ("done", "failed", "cancelled"):
        return False
    _update(task_id, status="cancelled", completed_at=_now())
    # Signal the running coroutine if any
    _cancel_signals.add(task_id)
    return True


_cancel_signals: set[str] = set()

# Per-task progress event queues: {task_id: asyncio.Queue}
_progress_queues: dict[str, asyncio.Queue] = {}


def _task_queue(task_id: str) -> asyncio.Queue:
    if task_id not in _progress_queues:
        _progress_queues[task_id] = asyncio.Queue(maxsize=256)
    return _progress_queues[task_id]


# ── Execution engine ─────────────────────────────────────────────────────────

async def run_task(
    task_id: str,
    coro_fn: Callable[..., Awaitable[str]],
    *args,
    **kwargs,
) -> None:
    """
    Execute coro_fn(*args, **kwargs) as a background task.
    Progress can be reported via set_progress(task_id, msg).
    On completion, sends a push notification if configured.
    """
    task = get_task(task_id)
    if not task:
        log.error("[BGTASK] task %s not found", task_id)
        return

    _update(task_id, status="running", started_at=_now(), progress="0")
    q = _task_queue(task_id)

    try:
        result = await coro_fn(*args, **kwargs)
        if task_id in _cancel_signals:
            _cancel_signals.discard(task_id)
            _update(task_id, status="cancelled", completed_at=_now(), progress="100")
            await q.put({"type": "task_cancelled", "task_id": task_id})
        else:
            _update(task_id, status="done", completed_at=_now(), result=str(result), progress="100")
            await q.put({"type": "task_done", "task_id": task_id, "result": str(result)[:500]})
            await _notify_completion(task, result)
    except Exception as e:
        log.error("[BGTASK] %s failed: %s", task_id, e, exc_info=True)
        _update(task_id, status="failed", completed_at=_now(), error=str(e), progress="error")
        await q.put({"type": "task_failed", "task_id": task_id, "error": str(e)})
    finally:
        _cancel_signals.discard(task_id)
        # Keep queue alive for 60s so SSE consumers can drain it
        asyncio.get_event_loop().call_later(60, lambda: _progress_queues.pop(task_id, None))


def set_progress(task_id: str, progress: str) -> None:
    """Call from inside a background task to report progress (0–100 or text)."""
    _update(task_id, progress=progress)
    q = _progress_queues.get(task_id)
    if q:
        try:
            q.put_nowait({"type": "task_progress", "task_id": task_id, "progress": progress})
        except asyncio.QueueFull:
            pass
    if task_id in _cancel_signals:
        raise asyncio.CancelledError(f"Task {task_id} cancelled by user")


async def _notify_completion(task: dict, result: str) -> None:
    channel = task.get("notify_channel", "")
    title   = task.get("title", task["id"])
    msg     = f"**Task complete**: {title}\n{str(result)[:300]}"
    try:
        from . import config as _cfg
        if channel == "telegram" or (not channel and _cfg.get("telegram_notify_chat_id")):
            from .telegram.handler import send_reply as tg_send, is_configured as tg_ok
            chat_id = _cfg.get("telegram_notify_chat_id", "")
            if tg_ok() and chat_id:
                await tg_send(chat_id, msg)
        elif channel == "whatsapp":
            from .whatsapp.handler import send_reply as wa_send, is_configured as wa_ok
            wa_to = _cfg.get("whatsapp_notify_number", "")
            if wa_ok() and wa_to:
                await wa_send(wa_to, msg)
    except Exception as e:
        log.warning("[BGTASK] notification failed: %s", e)
