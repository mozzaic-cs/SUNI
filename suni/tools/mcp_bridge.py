"""
MCP Bridge — connects SUNI to external MCP servers.

Two config sources (merged at startup):
  1. claude_desktop_config.json  — Claude Desktop's config (read-only from SUNI admin)
  2. memory/mcp_servers.json     — SUNI-managed servers (writable from admin panel)

Tool names are prefixed: {server_name}_{tool_name}.
Skips the 'suni' server to avoid circular connections.
"""
from __future__ import annotations
import asyncio
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from rich.console import Console

console = Console(stderr=True)

CLAUDE_DESKTOP_CONFIG = Path(os.environ.get(
    "CLAUDE_CONFIG",
    str(Path.home() / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"),
))

# SUNI-managed server config — admin writes here, never to claude_desktop_config.json
SUNI_MCP_CONFIG = Path("memory/mcp_servers.json")

_SKIP = {"suni"}  # never connect back to ourselves


class _Connection:
    def __init__(self, name: str):
        self.name = name
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None

    async def connect(self, params: StdioServerParameters) -> None:
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except Exception:
            await stack.aclose()
            raise
        self._stack = stack
        self._session = session

    async def call_tool(self, name: str, args: dict) -> str:
        if not self._session:
            return f"[MCP] '{self.name}' server is not connected"
        try:
            result = await self._session.call_tool(name, args)
            parts = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif hasattr(block, "model_dump"):
                    parts.append(json.dumps(block.model_dump()))
                else:
                    parts.append(str(block))
            return "\n".join(parts) or "(no output)"
        except Exception as e:
            return f"[MCP] tool error: {e}"

    async def list_tools(self):
        if not self._session:
            raise RuntimeError("not connected")
        return await self._session.list_tools()

    async def close(self) -> None:
        if self._stack:
            try:
                await self._stack.aclose()
            except Exception:
                pass
            self._stack = None
            self._session = None


class MCPBridge:
    """
    Starts persistent connections to all mcpServers in Claude Desktop config
    and exposes their tools through SUNI's ToolRegistry.
    """

    def __init__(self, registry):
        self.registry = registry
        self._connections: dict[str, _Connection] = {}
        self.tool_count = 0

    def _load_all_servers(self) -> dict[str, tuple[dict, str]]:
        """Return merged {name: (cfg, source)} from both config files.
        SUNI config takes precedence over Claude Desktop config on name collision.
        source is "claude_desktop" or "suni".
        """
        merged: dict[str, tuple[dict, str]] = {}

        # Claude Desktop config (read-only — not managed by SUNI admin)
        try:
            cfg = json.loads(CLAUDE_DESKTOP_CONFIG.read_text(encoding="utf-8"))
            for name, scfg in cfg.get("mcpServers", {}).items():
                if name not in _SKIP:
                    merged[name] = (scfg, "claude_desktop")
        except Exception:
            pass

        # SUNI-managed config (writable from admin panel)
        try:
            cfg = json.loads(SUNI_MCP_CONFIG.read_text(encoding="utf-8"))
            for name, scfg in cfg.get("mcpServers", {}).items():
                if name not in _SKIP:
                    merged[name] = (scfg, "suni")
        except Exception:
            pass

        return merged

    async def start(self) -> None:
        servers = self._load_all_servers()
        if not servers:
            console.print("  [dim]MCP bridge: no external servers configured[/dim]")
            return

        console.print("  [dim]MCP bridge: connecting to servers...[/dim]")
        for name, (server_cfg, _source) in servers.items():
            await self._connect_server(name, server_cfg)

        if self._connections:
            console.print(
                f"  [bold green]MCP bridge ready:[/bold green] "
                f"{len(self._connections)} server(s), {self.tool_count} tool(s)"
            )

    async def _connect_server(self, name: str, cfg: dict) -> None:
        command = cfg.get("command", "")
        args = [str(a) for a in cfg.get("args", [])]
        env = cfg.get("env") or {}

        if not command:
            console.print(f"  [yellow]  ✗ {name}: no command specified[/yellow]")
            return

        params = StdioServerParameters(command=command, args=args, env=env or None)
        conn = _Connection(name)
        try:
            await asyncio.wait_for(conn.connect(params), timeout=20)
            tools_resp = await asyncio.wait_for(conn.list_tools(), timeout=10)
            tools = tools_resp.tools

            for tool in tools:
                self._register_tool(name, tool, conn)

            self._connections[name] = conn
            self.registry.register_mcp_prefix(name)
            console.print(f"  [green]  ✓ {name}[/green] [dim]({len(tools)} tools)[/dim]")

        except asyncio.TimeoutError:
            console.print(f"  [yellow]  ✗ {name}: timed out[/yellow]")
            await conn.close()
        except Exception as e:
            console.print(f"  [yellow]  ✗ {name}: {e}[/yellow]")
            await conn.close()

    def _register_tool(self, server: str, tool, conn: _Connection) -> None:
        prefixed = f"{server}_{tool.name}"
        schema = {
            "name": prefixed,
            "description": f"[{server}] {tool.description or tool.name}",
            "parameters": (
                tool.inputSchema
                if tool.inputSchema
                else {"type": "object", "properties": {}}
            ),
        }
        _conn, _name = conn, tool.name

        async def _handler(_c=_conn, _n=_name, **kwargs) -> str:
            return await _c.call_tool(_n, kwargs)

        self.registry.register(schema, _handler)
        self.tool_count += 1

    async def stop(self) -> None:
        for conn in list(self._connections.values()):
            await conn.close()
        self._connections.clear()

    # ── Management helpers (used by admin API) ────────────────────────────────

    async def restart(self) -> None:
        """Stop all connections, clear MCP tools from registry, reconnect."""
        await self.stop()
        self.registry.clear_mcp_tools()
        self.tool_count = 0
        await self.start()

    def update_config(self, new_servers: dict) -> None:
        """Write new mcpServers block to SUNI's own config (never touches claude_desktop_config.json)."""
        SUNI_MCP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        cfg = {"mcpServers": new_servers}
        tmp = SUNI_MCP_CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(SUNI_MCP_CONFIG)

    def _suni_servers(self) -> dict:
        """Return the current SUNI-managed server dict."""
        try:
            return json.loads(SUNI_MCP_CONFIG.read_text(encoding="utf-8")).get("mcpServers", {})
        except Exception:
            return {}

    async def test_server(self, name: str, cfg: dict) -> dict:
        """Connect to a server, list its tools, then disconnect. For validation."""
        conn = _Connection(name)
        try:
            params = StdioServerParameters(
                command=cfg.get("command", ""),
                args=[str(a) for a in cfg.get("args", [])],
                env=cfg.get("env") or None,
            )
            await asyncio.wait_for(conn.connect(params), timeout=15)
            resp  = await asyncio.wait_for(conn.list_tools(), timeout=10)
            tools = [t.name for t in resp.tools]
            await conn.close()
            return {"ok": True, "tools": tools, "tool_count": len(tools)}
        except asyncio.TimeoutError:
            await conn.close()
            return {"ok": False, "error": "Connection timed out"}
        except Exception as e:
            await conn.close()
            return {"ok": False, "error": str(e)}

    def server_status(self) -> list[dict]:
        """Return config + live status for every server from both config files.
        source="claude_desktop" servers are read-only in the admin UI.
        source="suni" servers are editable/deletable.
        """
        all_servers = self._load_all_servers()
        result = []
        for name, (scfg, source) in all_servers.items():
            tool_count = sum(1 for s in self.registry._schemas if s.startswith(name + "_"))
            env_keys   = list((scfg.get("env") or {}).keys())
            result.append({
                "name":       name,
                "command":    scfg.get("command", ""),
                "args":       scfg.get("args", []),
                "env_keys":   env_keys,
                "connected":  name in self._connections,
                "tool_count": tool_count,
                "source":     source,    # "claude_desktop" | "suni"
            })
        return result
