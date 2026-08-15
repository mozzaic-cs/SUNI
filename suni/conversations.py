"""
Persistent conversation storage for the Chat UI.

Tables:
  conversations — id, user_id, title, created_at, updated_at
  messages      — id, conversation_id, role, content, ts
"""
from __future__ import annotations
import sqlite3
import time
import uuid
from pathlib import Path

_DB = Path("memory/conversations.db")


def init_db() -> None:
    conn = sqlite3.connect(_DB)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            title       TEXT NOT NULL DEFAULT 'Nova conversa',
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id              TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role            TEXT NOT NULL,
            content         TEXT NOT NULL,
            ts              REAL NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at);
        CREATE INDEX IF NOT EXISTS idx_msg_conv  ON messages(conversation_id, ts);
    """)
    try:
        conn.execute("ALTER TABLE conversations ADD COLUMN cc_session_id TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    conn.close()


def _row_to_conv(r) -> dict:
    return {"id": r[0], "user_id": r[1], "title": r[2], "created_at": r[3], "updated_at": r[4]}


def list_conversations(user_id: str, limit: int = 200) -> list[dict]:
    conn = sqlite3.connect(_DB)
    rows = conn.execute(
        "SELECT id, user_id, title, created_at, updated_at "
        "FROM conversations WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [_row_to_conv(r) for r in rows]


def create_conversation(user_id: str, title: str = "Nova conversa") -> dict:
    conv_id = str(uuid.uuid4())
    now = time.time()
    conn = sqlite3.connect(_DB)
    conn.execute(
        "INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
        (conv_id, user_id, title, now, now),
    )
    conn.commit()
    conn.close()
    return {"id": conv_id, "user_id": user_id, "title": title, "created_at": now, "updated_at": now}


def get_conversation(conv_id: str, user_id: str) -> dict | None:
    conn = sqlite3.connect(_DB)
    row = conn.execute(
        "SELECT id, user_id, title, created_at, updated_at "
        "FROM conversations WHERE id = ? AND user_id = ?",
        (conv_id, user_id),
    ).fetchone()
    conn.close()
    return _row_to_conv(row) if row else None


def get_messages(conv_id: str) -> list[dict]:
    conn = sqlite3.connect(_DB)
    rows = conn.execute(
        "SELECT id, role, content, ts FROM messages "
        "WHERE conversation_id = ? ORDER BY ts ASC",
        (conv_id,),
    ).fetchall()
    conn.close()
    return [{"id": r[0], "role": r[1], "content": r[2], "ts": r[3]} for r in rows]


def add_message(conv_id: str, role: str, content: str) -> dict:
    msg_id = str(uuid.uuid4())
    now = time.time()
    conn = sqlite3.connect(_DB)
    conn.execute(
        "INSERT INTO messages (id, conversation_id, role, content, ts) VALUES (?,?,?,?,?)",
        (msg_id, conv_id, role, content, now),
    )
    conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id))
    conn.commit()
    conn.close()
    return {"id": msg_id, "role": role, "content": content, "ts": now}


def get_cc_session(conv_id: str) -> str | None:
    conn = sqlite3.connect(_DB)
    row = conn.execute(
        "SELECT cc_session_id FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def set_cc_session(conv_id: str, cc_session_id: str) -> None:
    conn = sqlite3.connect(_DB)
    conn.execute(
        "UPDATE conversations SET cc_session_id = ? WHERE id = ?", (cc_session_id, conv_id)
    )
    conn.commit()
    conn.close()


def update_title(conv_id: str, user_id: str, title: str) -> bool:
    conn = sqlite3.connect(_DB)
    c = conn.execute(
        "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
        (title, time.time(), conv_id, user_id),
    )
    conn.commit()
    ok = c.rowcount > 0
    conn.close()
    return ok


def delete_conversation(conv_id: str, user_id: str) -> bool:
    conn = sqlite3.connect(_DB)
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.execute(
        "DELETE FROM conversations WHERE id = ? AND user_id = ?",
        (conv_id, user_id),
    )
    conn.commit()
    ok = c.rowcount > 0
    conn.close()
    return ok
