"""Network reachability tools — ping_host."""
from __future__ import annotations
import asyncio
import platform
import re
import socket

SCHEMA = {
    "name": "ping_host",
    "description": (
        "Check whether a hostname or IP address is reachable on the network. "
        "Use this whenever the user asks to ping a host, check if a server is up, "
        "or test network reachability. Returns ALIVE with latency, or UNREACHABLE."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "host": {
                "type": "string",
                "description": "Hostname or IP address to test (e.g. 192.168.1.150 or google.com)",
            },
            "count": {
                "type": "integer",
                "description": "Number of pings to send (default 4, max 10)",
            },
        },
        "required": ["host"],
    },
}


def _parse_ping_output(output: str) -> dict:
    """Extract packet loss and round-trip stats from platform ping output."""
    # Windows: "Packets: Sent = 4, Received = 4, Lost = 0 (0% loss)"
    # Linux:   "4 packets transmitted, 4 received, 0% packet loss"
    loss_match = re.search(r'(\d+)%\s*(?:packet\s*)?loss', output, re.IGNORECASE)
    loss_pct = int(loss_match.group(1)) if loss_match else None

    # Windows: "Minimum = 1ms, Maximum = 2ms, Average = 1ms"
    # Linux:   "rtt min/avg/max/mdev = 0.1/0.2/0.3/0.1 ms"
    win_avg = re.search(r'Average\s*=\s*(\d+)ms', output, re.IGNORECASE)
    lin_avg = re.search(r'min/avg/max.*?=\s*[\d.]+/([\d.]+)/', output)
    avg_ms = None
    if win_avg:
        avg_ms = int(win_avg.group(1))
    elif lin_avg:
        avg_ms = round(float(lin_avg.group(1)))

    return {"loss_pct": loss_pct, "avg_ms": avg_ms}


async def handler(host: str, count: int = 4) -> str:
    count = min(max(1, int(count)), 10)
    is_windows = platform.system() == "Windows"

    # Build OS-appropriate command
    if is_windows:
        cmd = ["ping", "-n", str(count), host]
    else:
        cmd = ["ping", "-c", str(count), "-W", "2", host]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=count * 5 + 10)
        except asyncio.TimeoutError:
            proc.kill()
            return f"UNREACHABLE — ping timed out after {count * 5 + 10}s."

        output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
        stats = _parse_ping_output(output)

        # Determine result
        if proc.returncode == 0 and (stats["loss_pct"] is None or stats["loss_pct"] < 100):
            latency = f" · avg {stats['avg_ms']}ms" if stats["avg_ms"] is not None else ""
            loss = f" · {stats['loss_pct']}% loss" if stats["loss_pct"] else ""
            return f"ALIVE — {host} is responding to pings{latency}{loss}."
        else:
            loss = f" ({stats['loss_pct']}% loss)" if stats["loss_pct"] is not None else ""
            return f"UNREACHABLE — {host} did not respond to ping{loss}."

    except FileNotFoundError:
        # ping not in PATH — fall back to socket probe on port 80/443
        return await _socket_probe(host)
    except Exception as e:
        return f"Error running ping: {e}"


async def _socket_probe(host: str) -> str:
    """TCP reachability probe as fallback when ping binary is unavailable."""
    for port in (80, 443, 22):
        try:
            loop = asyncio.get_event_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: socket.create_connection((host, port), timeout=3)),
                timeout=5,
            )
            return f"ALIVE — {host} is reachable (TCP port {port} open)."
        except (socket.timeout, ConnectionRefusedError, OSError):
            continue
    return f"UNREACHABLE — {host} did not respond on ports 80/443/22."
