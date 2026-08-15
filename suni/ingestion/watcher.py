"""
Background session watcher — polls ~/.claude/projects/ for new/updated
JSONL files every POLL_INTERVAL seconds and auto-ingests them.

Runs as a background asyncio task; does not block the main REPL.
"""
from __future__ import annotations
import asyncio
from rich.console import Console
from .claude_code import find_new_or_updated, _load_state, ingest_all

console = Console(stderr=True)
POLL_INTERVAL = 60  # seconds


async def watch(memory_manager, stop_event: asyncio.Event) -> None:
    """
    Continuously poll for new Claude Code sessions until stop_event is set.
    Designed to run as a background asyncio task.
    """
    console.print("[dim]Session watcher started (60s interval).[/dim]")
    while not stop_event.is_set():
        try:
            state = _load_state()
            new_sessions = find_new_or_updated(state)
            if new_sessions:
                console.print(
                    f"[dim]Watcher: found {len(new_sessions)} new session(s), ingesting...[/dim]"
                )
                stats = await ingest_all(memory_manager)
                console.print(
                    f"[dim]Watcher: +{stats['chunks']} memories from "
                    f"{stats['sessions']} session(s)[/dim]"
                )
        except Exception as e:
            console.print(f"[dim yellow]Watcher error: {e}[/dim yellow]")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass  # normal — just keep looping
