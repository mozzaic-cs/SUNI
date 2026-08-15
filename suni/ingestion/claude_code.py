"""
Ingests Claude Code session JSONL files into Suni's RAG memory.

Session files live at: ~/.claude/projects/<project>/<session-id>.jsonl
Each line is a JSON entry with type: user | assistant | system | ...

Strategy:
  - Extract user+assistant text messages (skip tool calls/results)
  - Group into chunks of N conversation pairs per memory entry
  - Track ingested files by path+mtime so re-runs are idempotent
"""
from __future__ import annotations
import json
import os
import glob
from pathlib import Path
from datetime import datetime

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _decode_project_dir(name: str) -> str:
    """Turn a Claude Code project directory name back into a path.

    Claude Code encodes the project path by replacing separators with dashes,
    and the result differs by platform:

        Windows   C:\\suni              ->  C--suni
        POSIX     /home/user/proj    ->  -home-user-proj

    Decoding only the Windows form (replace("C--", "C:/")) left POSIX names
    mangled — every ingested session was labelled with a dash-run instead of
    its path. The label is used for attribution on stored memories, so it is
    cosmetic but wrong everywhere except one drive letter on one platform.
    """
    if len(name) > 3 and name[0].isalpha() and name[1:3] == "--":
        # Drive-letter form: the first "--" is the colon+separator.
        return f"{name[0]}:/" + name[3:].replace("--", "/")
    if name.startswith("-"):
        # POSIX absolute form: a leading separator, then one dash per separator.
        return "/" + name[1:].replace("-", "/")
    return name.replace("--", "/")
INGESTION_STATE_FILE = Path("memory/ingestion_state.json")
CHUNK_PAIRS = 4  # conversation pairs per memory entry
MIN_TEXT_LEN = 20  # skip very short messages


def _load_state() -> dict:
    if INGESTION_STATE_FILE.exists():
        return json.loads(INGESTION_STATE_FILE.read_text(encoding="utf-8"))
    return {}


def _save_state(state: dict) -> None:
    INGESTION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    INGESTION_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _extract_text(content) -> str:
    """Extract plain text from a message content field (str or list of blocks)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                # Only take text blocks; skip tool_use, tool_result, image, etc.
                if block.get("type") == "text":
                    parts.append(block.get("text", "").strip())
        return " ".join(parts)
    return ""


def extract_messages(jsonl_path: str) -> list[dict]:
    """
    Parse a session JSONL and return clean user/assistant message pairs.
    Filters out tool-call noise, very short messages, and system messages.
    """
    messages: list[dict] = []
    with open(jsonl_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            role = entry.get("type")
            if role not in ("user", "assistant"):
                continue

            # Message content lives inside entry.message.content
            msg = entry.get("message", {})
            if not isinstance(msg, dict):
                continue

            text = _extract_text(msg.get("content", ""))
            if len(text) < MIN_TEXT_LEN:
                continue

            messages.append({"role": role, "text": text})

    return messages


def chunk_into_memories(messages: list[dict], project: str, session_id: str) -> list[dict]:
    """
    Group messages into chunks of CHUNK_PAIRS conversation pairs.
    Each chunk becomes one memory entry.
    """
    chunks: list[dict] = []
    current: list[str] = []
    pair_count = 0

    for i, msg in enumerate(messages):
        prefix = "User" if msg["role"] == "user" else "Suni(Claude)"
        # Truncate to keep memory entries concise
        text = msg["text"][:500]
        current.append(f"{prefix}: {text}")

        # Count a pair when we see an assistant message following a user message
        if msg["role"] == "assistant":
            pair_count += 1

        if pair_count >= CHUNK_PAIRS:
            chunks.append({
                "content": "\n".join(current),
                "project": project,
                "session": session_id,
                "type": "conversation",
            })
            current = []
            pair_count = 0

    if current:
        chunks.append({
            "content": "\n".join(current),
            "project": project,
            "session": session_id,
            "type": "conversation",
        })

    return chunks


def discover_sessions() -> list[dict]:
    """Return all session JSONL files with their project names and mtimes."""
    sessions = []
    if not CLAUDE_PROJECTS_DIR.exists():
        return sessions

    for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        project_name = _decode_project_dir(project_dir.name)

        for jsonl_path in project_dir.glob("*.jsonl"):
            sessions.append({
                "path": str(jsonl_path),
                "project": project_name,
                "session_id": jsonl_path.stem,
                "mtime": jsonl_path.stat().st_mtime,
            })

    return sessions


def find_new_or_updated(state: dict) -> list[dict]:
    """Return sessions not yet ingested or updated since last ingest."""
    all_sessions = discover_sessions()
    return [
        s for s in all_sessions
        if s["path"] not in state or state[s["path"]] < s["mtime"]
    ]


async def ingest_all(memory_manager, force: bool = False) -> dict:
    """
    Ingest all new or updated Claude Code sessions into Suni's memory.
    Returns stats: {ingested_sessions, ingested_chunks, skipped}.
    """
    state = {} if force else _load_state()
    sessions = find_new_or_updated(state) if not force else discover_sessions()

    stats = {"sessions": 0, "chunks": 0, "skipped": 0}

    for session in sessions:
        messages = extract_messages(session["path"])
        if not messages:
            stats["skipped"] += 1
            continue

        chunks = chunk_into_memories(
            messages, session["project"], session["session_id"]
        )

        for chunk in chunks:
            await memory_manager.add(
                chunk["content"],
                memory_type="conversation",
                metadata={"project": chunk["project"], "session": chunk["session"]},
            )

        state[session["path"]] = session["mtime"]
        stats["sessions"] += 1
        stats["chunks"] += len(chunks)

    _save_state(state)
    return stats
