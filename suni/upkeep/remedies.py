"""Upkeep phase 2: a short, fixed list of things SUNI may fix on her own.

Each remedy answers one incident code, does one thing that is harmless to do
twice, then checks the incident's own probe again. Nothing here writes config
or anything under memory/ except upkeep's own book, and nothing stops or kills
a program: an Ollama that is running but not answering is the user's program,
and it goes to a person.

upkeep_level decides how far it goes:
  0  off.
  1  dry run: the book says what it WOULD do, under the same limits, and
     nothing is touched. This is how to watch it before trusting it.
  2  act.

Limits: at most `max_tries` attempts per incident in `window_hours`. When they
run out, or a remedy refuses (the problem is not one it may touch), the
incident stays open with a note for a person, and upkeep stops trying.

What is deliberately NOT here: restarting SUNI herself. No finding today calls
for it — a dead listener already ends the process for the supervisor (see
listener_watchdog) — and a restart launched from inside the scheduled task is
likely killed along with it, halfway. It comes back with phase 4, which needs
it for deploys, behind idle.busy_reasons().
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from . import health, idle, store

log = logging.getLogger("suni.upkeep")


@dataclass(frozen=True)
class Remedy:
    name: str
    max_tries: int
    window_hours: float
    needs_idle: bool


RESTART_CHANNEL = Remedy("restart_channel", max_tries=3, window_hours=6, needs_idle=False)
START_OLLAMA = Remedy("start_ollama", max_tries=2, window_hours=6, needs_idle=False)

CHANNEL_RECHECK_S = 15
OLLAMA_RECHECK_S = 45
OLLAMA_POLL_S = 3


class Refused(Exception):
    """Not something upkeep may fix: say why, and leave it to a person."""


def match(code: str) -> tuple[Remedy, str] | None:
    parts = code.split(".")
    if len(parts) == 3 and parts[0] == "channel" and parts[2] == "down":
        return RESTART_CHANNEL, parts[1]
    if code == "ollama.down":
        return START_OLLAMA, ""
    return None


def describe(remedy: Remedy, target: str) -> str:
    return {"restart_channel": f"restart the {target} channel",
            "start_ollama": "start Ollama"}[remedy.name]


# ── the actions ──────────────────────────────────────────────────────────────

# The server registers this: async (name) -> None, its own _start_channel.
_restart_channel: Callable[[str], Awaitable[None]] | None = None


def register_channel_restart(fn: Callable[[str], Awaitable[None]] | None) -> None:
    global _restart_channel
    _restart_channel = fn


async def _do_restart_channel(target: str) -> str:
    if _restart_channel is None:
        raise Refused("the server has not given upkeep a way to restart channels")
    if not health.channel_status().get(target, {}).get("should"):
        raise Refused(f"{target} is no longer switched on")
    await _restart_channel(target)
    await asyncio.sleep(CHANNEL_RECHECK_S)
    st = health.channel_status().get(target, {})
    if st.get("running"):
        return f"{target} restarted and still running after {CHANNEL_RECHECK_S}s"
    raise RuntimeError(f"{target} stopped again within {CHANNEL_RECHECK_S}s "
                       f"{st.get('error', '')}".strip())


def _ollama_processes() -> list[str]:
    import psutil
    names = []
    for p in psutil.process_iter(["name"]):
        n = (p.info.get("name") or "").lower()
        if n.startswith("ollama"):
            names.append(n)
    return names


def _ollama_app() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"


def _spawn_detached(path: Path) -> None:
    import subprocess
    from .. import proc
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True)
    try:
        # Out of SUNI's job, so stopping SUNI's task does not take Ollama too.
        proc.popen([str(path)], creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:
        proc.popen([str(path)], creationflags=flags, **kw)


async def _do_start_ollama(_target: str) -> str:
    addr = health.ollama_address()
    if addr is None:
        raise Refused("memory does not use a local Ollama any more")
    if await asyncio.to_thread(health.listening, *addr):
        return "Ollama was already answering"
    if sys.platform != "win32":
        raise Refused("start Ollama with this machine's service manager; upkeep only "
                      "starts the Windows desktop app")
    running = await asyncio.to_thread(_ollama_processes)
    if running:
        raise Refused(f"Ollama is running ({', '.join(sorted(set(running)))}) but not "
                      f"answering; upkeep does not stop programs it did not start")
    app = _ollama_app()
    if not app.exists():
        raise Refused(f"the Ollama app is not at {app}")
    await asyncio.to_thread(_spawn_detached, app)
    for _ in range(max(1, int(OLLAMA_RECHECK_S // max(OLLAMA_POLL_S, 1)))):
        await asyncio.sleep(OLLAMA_POLL_S)
        if await asyncio.to_thread(health.listening, *addr):
            return "started the Ollama app; it is answering"
    raise RuntimeError(f"started the Ollama app but nothing answered on "
                       f"{addr[0]}:{addr[1]} within {OLLAMA_RECHECK_S}s")


ACTIONS: dict[str, Callable[[str], Awaitable[str]]] = {
    "restart_channel": _do_restart_channel,
    "start_ollama": _do_start_ollama,
}


# ── the loop ─────────────────────────────────────────────────────────────────

def _hand_over(inc: dict, mode: str, remedy: Remedy, target: str, why: str) -> None:
    store.add_action(inc["id"], remedy.name, target, mode, "gave_up", why)
    if mode == "act":
        store.set_state(inc["id"], "open", f"needs a person: {why}", by="upkeep")


async def run(level: int) -> list[dict]:
    """One pass over the open incidents. Returns what it did, for the log."""
    if level <= 0:
        return []
    mode = "act" if level >= 2 else "dry"
    done: list[dict] = []
    busy = idle.busy_reasons()
    for inc in await asyncio.to_thread(store.list_incidents, "finding"):
        if inc["state"] != "open":
            continue
        m = match(inc["code"])
        if not m:
            continue
        remedy, target = m
        what = describe(remedy, target)
        past = await asyncio.to_thread(store.actions_since, inc["id"],
                                       remedy.window_hours, mode)
        if past and past[-1]["outcome"] in ("gave_up", "refused"):
            continue                                  # already with a person
        tries = sum(1 for a in past if a["outcome"] in store.TRIES)
        if tries >= remedy.max_tries:
            _hand_over(inc, mode, remedy, target,
                       f"tried to {what} {tries} times in {remedy.window_hours:g}h "
                       f"and it keeps coming back")
            done.append({"incident": inc["id"], "outcome": "gave_up"})
            continue
        if remedy.needs_idle and busy:
            continue                                  # next scan; nothing recorded
        if mode == "dry":
            store.add_action(inc["id"], remedy.name, target, mode, "would", f"would {what}")
            done.append({"incident": inc["id"], "outcome": "would"})
            continue
        log.info("[UPKEEP] %s for %s", what, inc["id"])
        try:
            detail = await ACTIONS[remedy.name](target)
        except Refused as exc:
            store.add_action(inc["id"], remedy.name, target, mode, "refused", str(exc))
            store.set_state(inc["id"], "open", f"needs a person: {exc}", by="upkeep")
            done.append({"incident": inc["id"], "outcome": "refused"})
            continue
        except Exception as exc:      # noqa: BLE001 — a failed fix is a result, not a crash
            store.add_action(inc["id"], remedy.name, target, mode, "failed",
                             str(exc)[:300] or exc.__class__.__name__)
            done.append({"incident": inc["id"], "outcome": "failed"})
            continue
        store.add_action(inc["id"], remedy.name, target, mode, "fixed", detail)
        store.set_state(inc["id"], "resolved", f"fixed by upkeep: {detail}", by="upkeep")
        done.append({"incident": inc["id"], "outcome": "fixed"})
    return done
