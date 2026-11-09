"""Two things diagnostics cannot see, checked for upkeep's fixes.

diagnostics reads config and logs. It cannot see that a channel's poller has
died while its switch is still on, because the channel tasks live inside the
server; nor does it ask whether the local Ollama is listening at all. These are
the two problems upkeep has a fix for, so they are the two it looks for.

Read-only like the rest of the scan: a TCP connect and a look at task state.
"""
from __future__ import annotations

import socket
from typing import Callable
from urllib.parse import urlparse

from ..diagnostics import BROKEN, Finding

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# The server registers this: () -> {name: {"should": bool, "running": bool}}.
# The tasks are in its closure; this is the one window onto them.
_channel_status: Callable[[], dict] | None = None


def register_channel_status(fn: Callable[[], dict] | None) -> None:
    global _channel_status
    _channel_status = fn


def channel_status() -> dict:
    if _channel_status is None:
        return {}
    try:
        return dict(_channel_status())
    except Exception:      # noqa: BLE001
        return {}


def ollama_address() -> tuple[str, int] | None:
    """Where memory's embeddings expect a LOCAL Ollama, or None when they do
    not use one here — a remote Ollama is not this machine's to start."""
    try:
        from .. import config as _cfg
        if (_cfg.get("embed_backend", "ollama") or "ollama") != "ollama":
            return None
        base = _cfg.get("embed_base_url", "") or _cfg.ollama_host()
    except Exception:      # noqa: BLE001
        return None
    u = urlparse(base if "://" in base else "http://" + base)
    if (u.hostname or "") not in LOCAL_HOSTS:
        return None
    return u.hostname, u.port or 11434


def listening(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def findings() -> list[Finding]:
    out: list[Finding] = []
    for name, st in sorted(channel_status().items()):
        if st.get("should") and not st.get("running"):
            out.append(Finding(
                BROKEN, f"{name} is switched on but its connection is not running",
                st.get("error", ""), "restart the channel",
                code=f"channel.{name}.down"))
    addr = ollama_address()
    if addr and not listening(*addr):
        out.append(Finding(
            BROKEN, "the local Ollama is not answering, so memory cannot embed",
            f"nothing listening on {addr[0]}:{addr[1]}", "start Ollama",
            code="ollama.down"))
    return out
