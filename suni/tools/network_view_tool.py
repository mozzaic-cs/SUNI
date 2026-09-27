"""Let SUNI point at the field behind her.

The Face draws a network of everything she can reach — the machine, the models
and services, the skills and agents, the indexed drives. Until now only the
viewer could move through it: she could describe the machines on the network in
prose while the picture behind her head sat on the overview, showing none of
them.

This is the other half. Asked "which devices are on the network", she can take
the view to the machines, group them, and light them up, and then say what is
there. The answer is the same answer either way — the tool returns the text and
the reply stands on its own — but the picture agrees with it.

Two things it deliberately is not:

  It is not discovery. Every level it can show is read from what the graph
  already knows; nothing here probes, scans or pings anything, and the machine
  list comes from the ARP cache the OS already has. If neighbour discovery is
  switched off, it says so plainly rather than showing an empty view and
  leaving the viewer to wonder whether the network is empty or the feature is.

  It is not consequential. Changing what is on screen is not an action that
  needs approving; an approval card in front of every "mostra-me a rede" would
  kill the feature. Opening a file from the field is a different matter and
  lives behind its own gate.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("suni.tools.netview")

# What a person might call each level, in both languages the interface speaks.
# The graph's own ids are terse ("kb", "network"); these are what gets asked for.
VIEWS = {
    "overview":  ("overview",  "everything at once"),
    "machines":  ("network",   "this machine and its neighbours"),
    "network":   ("network",   "this machine and its neighbours"),
    "devices":   ("network",   "this machine and its neighbours"),
    "models":    ("models",    "the models she can think with"),
    "tools":     ("tools",     "the tools she can call"),
    "skills":    ("skills",    "the skills she has been taught"),
    "channels":  ("channels",  "the channels she is reachable on"),
    "agents":    ("agents",    "the named agents and their schedules"),
    "files":     ("kb",        "the indexed drives and folders"),
    "documents": ("kb",        "the indexed drives and folders"),
    "knowledge": ("kb",        "the indexed drives and folders"),
}

# Which node category a level is mostly made of, for the spotlight. Lighting up
# "machine" on the models level would dim everything and light nothing.
SPOT_FOR = {
    "network": "machine", "models": "model", "tools": "tool",
    "skills": "skill", "channels": "channel", "agents": "agent", "kb": "folder",
    # "agents" is the one level with two kinds in it - the named agents and the
    # scheduled runs - and the agents are what was asked for.
}

SCHEMA = {
    "name": "show_network",
    "description": (
        "Move the 3D field behind SUNI's head to a particular view, so the user "
        "can see what you are talking about. Use it when they ask to see, show, "
        "or list what is on the machine or the network — devices, models, tools, "
        "skills, channels, agents, indexed files — and when a visual answer is "
        "clearer than a list. It changes the picture and returns what is in it, "
        "so answer from what comes back."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "view": {
                "type": "string",
                "description": (
                    "What to show: overview, machines, models, tools, skills, "
                    "channels, agents, or files."
                ),
                "enum": sorted({v for v in VIEWS}),
            },
            "group": {
                "type": "boolean",
                "description": (
                    "Pull the field apart into one cluster per category. Useful "
                    "when the question is about how much of what there is."
                ),
                "default": False,
            },
        },
        "required": ["view"],
    },
}


# The same stores the /api/graph route builds from. Handed over at startup,
# the way kb_tool is handed the document store: graph.build() with none of them
# returns a nearly empty level, which would have looked exactly like a machine
# with nothing on it.
_SOURCES: dict = {}


def set_sources(**kw) -> None:
    _SOURCES.update(kw)


def _emit(level: str, *, group: bool, spot: str | None) -> bool:
    """Tell the Face to go there. False if nothing is listening.

    The reply never depends on this landing: a headless caller — Telegram, a
    schedule, the API — gets the same text with no picture to go with it.
    """
    try:
        from .claude_code_advanced import EVENT_CB_CTX
        cb = EVENT_CB_CTX.get()
    except Exception:                      # noqa: BLE001 — no event channel here
        cb = None
    if not cb:
        return False
    try:
        cb({"type": "net_view", "level": level, "cluster": bool(group), "spot": spot})
        return True
    except Exception as exc:               # noqa: BLE001 — a dead UI is not an error
        _log.debug("[NETVIEW] could not reach the Face: %s", exc)
        return False


def handler(view: str = "overview", group: bool = False) -> str:
    from .. import graph as _graph
    from ..tools.registry import USER_ID_CTX

    key = (view or "overview").strip().lower()
    level, what = VIEWS.get(key, VIEWS["overview"])
    user_id = USER_ID_CTX.get("")

    try:
        from .. import config as _cfg
        data = _graph.build(
            level,
            doc_store=_SOURCES.get("doc_store"),
            registry=_SOURCES.get("registry"),
            skill_store=_SOURCES.get("skill_store"),
            config=_cfg.all(),
            user_id=user_id,
            user_role=_SOURCES.get("role_of", lambda _u: "")(user_id),
        )
    except Exception as exc:               # noqa: BLE001 — never crash the turn
        _log.warning("[NETVIEW] %s failed: %s", level, exc)
        return f"Could not read the {key} view: {exc}"

    nodes = data.get("nodes") or []
    spot = SPOT_FOR.get(level) if level != "overview" else None
    shown = _emit(level, group=group, spot=spot)

    lines = [f"Showing {what} ({len(nodes)} node(s))."
             if shown else
             f"{what.capitalize()} — {len(nodes)} node(s). "
             f"(No Face open to show it on, so describe it instead.)"]

    # The empty case is the one that matters. A view with nothing in it because
    # a setting is off looks exactly like a view with nothing in it because
    # there is nothing there, and the second is a much worse thing to tell
    # somebody who has seventeen machines on their LAN.
    if level == "network" and len([n for n in nodes if n.get("kind") == "machine"]) <= 1:
        from .. import config as _cfg
        if not _cfg.get("network_neighbours", False):
            lines.append(
                "Only this machine is listed: neighbour discovery is switched "
                "off, so SUNI does not read the ARP cache and cannot name the "
                "other devices. It is the 'network_neighbours' setting in the "
                "admin panel; nothing is scanned either way, the OS already has "
                "the list.")
    if not nodes:
        lines.append("There is nothing at this level.")

    for n in nodes[:40]:
        bits = [str(n.get("label") or n.get("id"))]
        if n.get("detail"):
            bits.append(str(n["detail"]))
        elif n.get("count"):
            bits.append(f"{n['count']:,} inside")
        lines.append("  - " + " — ".join(bits))
    if len(nodes) > 40:
        lines.append(f"  ... and {len(nodes) - 40} more")

    lines.append("")
    lines.append("Answer from this list. The user is looking at the same thing, "
                 "so name what is there rather than saying you opened a view.")
    return chr(10).join(lines)
