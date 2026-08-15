"""
Suni MCP Server — exposes Suni's memory and tools to any MCP client.

Works with Claude Desktop, Claude Code (--mcp-config), or any MCP client.
Register it with your client. Claude Desktop's config lives at
%APPDATA%/Claude/claude_desktop_config.json on Windows,
~/Library/Application Support/Claude/claude_desktop_config.json on macOS and
~/.config/Claude/claude_desktop_config.json on Linux; Claude Code reads
~/.claude/settings.json. Use absolute paths for both entries:
  {
    "mcpServers": {
      "suni": {
        "command": "/absolute/path/to/python",
        "args": ["/absolute/path/to/suni_mcp.py"]
      }
    }
  }

Restart the client after adding. Exposes:
  - search_memory     : semantic search in Suni's persistent RAG
  - add_memory        : write new facts/preferences into Suni's memory
  - list_recent_memory: see what Suni remembers recently
  - chat_with_suni    : ask Suni (Ollama) a question locally
  - run_shell         : run a shell command (same denylist as Suni's main shell tool)
  - read_file         : read a local file
  - write_file        : write a local file
"""
from __future__ import annotations
import sys
import os
import asyncio

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(__file__))

from mcp.server.fastmcp import FastMCP
from suni.memory.manager import MemoryManager
import ollama

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

MEMORY_PATH = os.environ.get("SUNI_MEMORY_PATH", "memory/suni_memory.json")
EMBED_MODEL = os.environ.get("SUNI_EMBED_MODEL", "qwen2.5:1.5b")
CHAT_MODEL  = os.environ.get("SUNI_MODEL",       "qwen2.5:7b")

mcp = FastMCP("Suni")
memory = MemoryManager(store_path=MEMORY_PATH, embed_model=EMBED_MODEL)

# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_memory(query: str, top_k: int = 5) -> str:
    """
    Search Suni's persistent memory for information relevant to the query.
    Returns the most semantically similar stored memories.

    Args:
        query: What to search for (natural language)
        top_k: Number of results to return (default 5)
    """
    results = await memory.search(query, top_k=top_k)
    if not results:
        return "No relevant memories found."
    lines = [
        f"[{r['type'].upper()}] ({r['timestamp'][:10]}) {r['content']}"
        for r in results
    ]
    return "\n\n".join(lines)


@mcp.tool()
async def add_memory(content: str, memory_type: str = "fact") -> str:
    """
    Add a new entry to Suni's persistent memory.

    Args:
        content: The information to remember
        memory_type: One of: fact, preference, task, conversation
    """
    valid_types = ("fact", "preference", "task", "conversation")
    if memory_type not in valid_types:
        memory_type = "fact"
    mid = await memory.add(content, memory_type=memory_type)
    return f"Stored memory [{memory_type}] — id: {mid}"


@mcp.tool()
async def list_recent_memory(n: int = 10) -> str:
    """
    Show the N most recently added memory entries.

    Args:
        n: How many entries to return (default 10)
    """
    entries = memory.store.get_recent(n)
    if not entries:
        return "Memory is empty."
    lines = [
        f"[{e['type'].upper()}] ({e['timestamp'][:10]}) {e['content'][:120]}"
        for e in entries
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Local inference
# ---------------------------------------------------------------------------

@mcp.tool()
async def chat_with_suni(message: str, system: str = "") -> str:
    """
    Send a message to Suni (running locally on Ollama) and get a response.
    Useful for quick local inference without using Claude's API tokens.

    Args:
        message: The message to send to Suni
        system: Optional system prompt override
    """
    client = ollama.AsyncClient()
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})

    # Enrich with relevant memories
    mem_ctx = await memory.build_context(message, top_k=3)
    if mem_ctx:
        msgs.append({"role": "system", "content": mem_ctx})

    msgs.append({"role": "user", "content": message})

    response = await client.chat(model=CHAT_MODEL, messages=msgs)
    return response.message.content or "(no response)"


# ---------------------------------------------------------------------------
# Local system tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def run_shell(command: str, timeout: int = 30) -> str:
    """
    Execute a shell command on the local machine and return the output.
    Uses the same denylist as SUNI's main shell tool — destructive commands are blocked.

    Args:
        command: Shell command to run
        timeout: Timeout in seconds (default 30)
    """
    from suni.tools.shell_tool import _DENYLIST, _MAX_OUTPUT_BYTES
    if _DENYLIST.search(command):
        return ("Error: command blocked — matches a destructive pattern. "
                "Use claude_task for system changes that require elevated care.")
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"Error: timed out after {timeout}s"
        out = (stdout or b"")[:_MAX_OUTPUT_BYTES].decode(errors="replace")
        err = (stderr or b"")[:_MAX_OUTPUT_BYTES].decode(errors="replace")
        return (out + ("\n[stderr]\n" + err if err else "")).strip() or "(no output)"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def read_file(path: str) -> str:
    """
    Read the contents of a local file.

    Args:
        path: Absolute or relative file path
    """
    from pathlib import Path
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception as e:
        return f"Error reading '{path}': {e}"


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """
    Write content to a local file (creates directories as needed).

    Args:
        path: File path to write
        content: Text content to write
    """
    from pathlib import Path
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {path}"
    except Exception as e:
        return f"Error writing '{path}': {e}"


# ---------------------------------------------------------------------------
# SUNIverse article tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_recent_articles(n: int = 10, category_id: int = 0) -> str:
    """
    Get SUNI's most recently published articles from the SUNIverse database.

    Args:
        n: Number of articles to return (default 10, max 30)
        category_id: Filter by category (0=all, 1=AI & Society, 2=Digital Health,
                     3=Future of Work, 4=Privacy & Identity, 5=Connected Living,
                     6=Science & Discovery, 7=Digital Culture, 8=Ethics & Power,
                     9=Education & Learning, 10=Money & Economy, 11=Gaming, 12=Art & AI)
    """
    from suni.tools.articles_tool import get_recent_articles as _fn
    return _fn(n=n, category_id=category_id if category_id else None)


@mcp.tool()
def search_articles(query: str, limit: int = 8) -> str:
    """
    Search SUNI's published articles by keyword (searches titles and subtitles).

    Args:
        query: Keyword or phrase to search for
        limit: Max results to return (default 8)
    """
    from suni.tools.articles_tool import search_articles as _fn
    return _fn(query=query, limit=limit)


@mcp.tool()
def get_article_stats() -> str:
    """Get statistics about SUNI's published article library."""
    from suni.tools.articles_tool import get_article_stats as _fn
    return _fn()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
