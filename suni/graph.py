"""What SUNI can reach, as a graph — one level at a time.

The Face renders this behind the head: the machine it runs on, the models and
services it can call, the skills it knows, and the documents it has indexed.
It is a view, not a new source of truth — every node here is something another
part of SUNI already owns, asked for its current state.

Two rules shape the shape of it:

ONE LEVEL PER REQUEST. The index holds 7,637 files across 2,304 folders; sending
that as a graph would be megabytes for a picture nobody can read. A request asks
about one focus and gets its children, the way a directory listing does.

CHILDREN ARE CAPPED. The busiest indexed folder holds 308 files. Past a hundred
or so, points stop being distinguishable and start being fog, so the rest are
summarised as one "+N more" node rather than silently dropped — a view that
quietly omits things is worse than one that says it is omitting them.

Nothing here reads the filesystem. Folders and files come from what the document
index already has, so this cannot become a way to enumerate a disk that SUNI was
never pointed at.
"""
from __future__ import annotations

import os
from typing import Any

MAX_CHILDREN = 120          # per level, before the remainder is summarised
_LABEL_MAX = 42

# kind → how the client draws it. Kept here so the palette is one decision.
KINDS = ("root", "machine", "model", "tool", "skill", "channel", "folder", "file")


def _label(text: str) -> str:
    text = str(text or "").strip()
    return text if len(text) <= _LABEL_MAX else text[:_LABEL_MAX - 1] + "…"


def _node(nid: str, label: str, kind: str, **extra) -> dict:
    n = {"id": nid, "label": _label(label), "kind": kind}
    n.update({k: v for k, v in extra.items() if v not in (None, "", 0)})
    return n


# ── the indexed tree, derived from the document index ────────────────────────

_tree_cache: dict[str, Any] = {"key": None, "dirs": {}, "files": {}}


def _tree(doc_store) -> tuple[dict, dict]:
    """{parent_dir: {child_dir: file_count}} and {parent_dir: [file_path]}.

    Rebuilt only when the index size changes: walking 77k metadata entries takes
    tens of milliseconds, which is fine once and wasteful on every hover.
    """
    if doc_store is None:
        return {}, {}
    try:
        key = (doc_store.count(), doc_store.file_count())
    except Exception:      # noqa: BLE001 — a picture is never worth a failed request
        return {}, {}
    if _tree_cache["key"] == key:
        return _tree_cache["dirs"], _tree_cache["files"]

    paths = {m.get("file_path", "") for m in getattr(doc_store, "_meta", {}).values()}
    paths.discard("")
    dirs: dict[str, dict[str, int]] = {}
    files: dict[str, list[str]] = {}
    under: dict[str, int] = {}        # folder -> files anywhere beneath it
    for p in paths:
        parent = os.path.dirname(p)
        files.setdefault(parent, []).append(p)
        # Walk the ancestors once, registering each folder with its parent and
        # crediting the file to every level above it. Comparing every folder
        # against every file instead would be 2,300 x 7,600 string tests per
        # rebuild, which is a visibly slow request for a picture.
        cur = parent
        while True:
            under[cur] = under.get(cur, 0) + 1
            up = os.path.dirname(cur)
            if not up or up == cur:
                break
            dirs.setdefault(up, {})[cur] = 0
            cur = up
    for parent, kids in dirs.items():
        for child in kids:
            kids[child] = under.get(child, 0)
    _tree_cache.update({"key": key, "dirs": dirs, "files": files, "under": under})
    return dirs, files


def _roots(dirs: dict, files: dict) -> list[str]:
    """Where the indexed tree starts, with pass-through folders collapsed.

    A drive root is its own parent ("D:\\" -> "D:\\"), so a plain "parent not
    indexed" test excluded everything and the knowledge level came back empty.

    The chain below a root is then collapsed while it has one child and no files
    of its own: three nodes reading a drive, a company and a "Projects" folder
    tell the viewer nothing that one node reading the whole path does not.
    """
    roots = [d for d in dirs
             if os.path.dirname(d) == d or os.path.dirname(d) not in dirs]
    out = []
    for r in roots:
        cur = r
        while True:
            kids = dirs.get(cur, {})
            if len(kids) == 1 and not files.get(cur):
                cur = next(iter(kids))
                continue
            break
        out.append(cur)
    return sorted(set(out))


def _cap(nodes: list[dict], focus: str) -> list[dict]:
    if len(nodes) <= MAX_CHILDREN:
        return nodes
    rest = len(nodes) - MAX_CHILDREN
    kept = nodes[:MAX_CHILDREN]
    kept.append(_node(f"more:{focus}", f"+{rest:,} more", "folder", more=rest))
    return kept



# ── hosts ────────────────────────────────────────────────────────────────────

def _hostname() -> str:
    import socket
    try:
        return socket.gethostname()
    except Exception:      # noqa: BLE001
        return "this machine"


def _lan_ip() -> str:
    """This machine's address on the LAN.

    Found by asking the routing table which interface would be used to reach a
    public address — no packet is sent, and gethostbyname(hostname) is no use
    because it often answers 127.0.0.1.
    """
    import socket
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:      # noqa: BLE001
        return ""
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:      # noqa: BLE001
                pass


def _host_of(url: str) -> str:
    """The host in a URL or host[:port] string, with any credentials dropped.

    Connection strings carry passwords. Everything here ends up on a screen, so
    only ever the host survives.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    if "@" in text:                      # user:pass@host
        text = text.rsplit("@", 1)[1]
    return text.split(":", 1)[0] if text.count(":") == 1 else text


def _neighbours() -> list[tuple[str, str]]:
    """Machines this one has recently spoken to, from the local ARP cache.

    The cache is READ, never filled: nothing here pings, sweeps or scans, so a
    machine appears only because this one already talked to it. Off unless the
    operator turns it on, because a list of everything on the network is a
    different kind of information from a list of SUNI's own services.
    """
    import re
    import subprocess
    out = []
    try:
        raw = subprocess.run(["arp", "-a"], capture_output=True, text=True,
                             timeout=6).stdout
    except Exception:      # noqa: BLE001 — no arp, no neighbours, no error
        return out
    for line in raw.splitlines():
        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F][0-9a-fA-F][-:][0-9a-fA-F:-]+)", line)
        if not m:
            continue
        ip, mac = m.group(1), m.group(2)
        if ip.endswith(".255") or ip.startswith("224.") or ip.startswith("239."):
            continue                      # broadcast and multicast are not machines
        out.append((ip, mac))
    return out[:60]


# ── the graph ────────────────────────────────────────────────────────────────

def build(focus: str = "root", *, doc_store=None, registry=None, skill_store=None,
          config=None, user_id: str = "", user_role: str = "") -> dict:
    """Nodes and edges for one focus, plus the trail back to the root."""
    focus = (focus or "root").strip() or "root"
    cfg = config or {}
    nodes: list[dict] = []
    trail: list[dict] = [{"id": "root", "label": "SUNI"}]

    if focus == "root":
        from . import system_profile as sp
        nodes = [
            _node("machine", f"{sp.CPU_CORES}-core · {sp.RAM_GB:.0f} GB RAM", "machine",
                  detail=f"{sp.VRAM_MB} MB VRAM"),
            _node("models", str(cfg.get("model") or "models"), "model"),
            _node("tools", f"{len(registry.names()) if registry else 0} tools", "tool"),
            _node("skills", "skills", "skill"),
            _node("channels", "channels", "channel"),
            _node("agents", "agents & schedules", "skill"),
            _node("network", _hostname(), "machine", detail=_lan_ip()),
        ]
        dirs, files = _tree(doc_store)
        if dirs or files:
            total = sum(len(v) for v in files.values())
            nodes.append(_node("kb", f"{total:,} indexed files", "folder", count=total))

    elif focus == "tools":
        trail.append({"id": "tools", "label": "tools"})
        nodes = _cap([_node(f"tool:{n}", n, "tool")
                      for n in sorted(registry.names() if registry else [])], focus)

    elif focus == "skills":
        trail.append({"id": "skills", "label": "skills"})
        names: list[str] = []
        try:
            names = sorted(s.get("name", s.get("slug", "?"))
                           for s in (skill_store.list() if skill_store else []))
        except Exception:      # noqa: BLE001
            names = []
        nodes = _cap([_node(f"skill:{n}", n, "skill") for n in names], focus)

    elif focus == "channels":
        trail.append({"id": "channels", "label": "channels"})
        for ch in ("telegram", "discord", "slack", "whatsapp", "email"):
            on = bool(cfg.get(f"{ch}_enabled") or cfg.get(f"{ch}_bot_token")
                      or cfg.get(f"{ch}_app_token"))
            nodes.append(_node(f"channel:{ch}", ch, "channel", live=1 if on else 0))

    elif focus == "models":
        trail.append({"id": "models", "label": "models"})
        seen: list[str] = []
        for tier in (cfg.get("model_chain") or []):
            m = str(tier.get("model") or "").strip()
            if m and m not in seen:
                seen.append(m)
                nodes.append(_node(f"model:{m}", m, "model",
                                   live=1 if tier.get("enabled") else 0))
        primary = str(cfg.get("model") or "").strip()
        if primary and primary not in seen:
            nodes.insert(0, _node(f"model:{primary}", primary, "model", live=1))

    elif focus == "agents":
        trail.append({"id": "agents", "label": "agents & schedules"})
        try:
            from . import agents as _ag
            for a in _ag.list_for_user(user_id or "", user_role or "admin"):
                nodes.append(_node(f"agent:{a['slug']}", a.get("name") or a["slug"], "skill",
                                   live=1 if a.get("enabled", True) else 0,
                                   detail=a.get("model") or ""))
        except Exception:      # noqa: BLE001 — a branch that cannot load is empty, not fatal
            pass
        try:
            from . import schedules as _sc
            for x in _sc.list_for_user(user_id or "", user_role or "admin"):
                nodes.append(_node(f"sched:{x['id']}", x.get("name") or x["id"], "channel",
                                   live=1 if x.get("enabled") else 0,
                                   detail=x.get("cadence") or ""))
        except Exception:      # noqa: BLE001
            pass
        nodes = _cap(nodes, focus)

    elif focus == "network":
        trail.append({"id": "network", "label": _hostname()})
        # Hosts SUNI actually talks to, read from configuration. Credentials are
        # stripped by _host_of: a connection string on a screen is a leak.
        seen: set[str] = set()

        def _host_node(host: str, what: str) -> None:
            host = (host or "").strip()
            if not host or host in seen:
                return
            seen.add(host)
            local = host in ("localhost", "127.0.0.1", "::1")
            nodes.append(_node(f"host:{host}", host, "machine",
                               detail=what + (" · this machine" if local else "")))

        _host_node(_host_of(cfg.get("ollama_host") or ""), "ollama")
        for ep in (cfg.get("ollama_endpoints") or []):
            _host_node(_host_of(ep if isinstance(ep, str) else ep.get("url", "")), "ollama")
        for key, what in (("vllm_base_url", "vLLM"), ("embed_base_url", "embeddings"),
                          ("stt_base_url", "speech in"), ("tts_base_url", "speech out"),
                          ("vision_base_url", "vision"), ("smtp_host", "mail out"),
                          ("imap_host", "mail in"), ("logship_host", "log shipping")):
            _host_node(_host_of(cfg.get(key) or ""), what)
        for tier in (cfg.get("model_chain") or []):
            _host_node(_host_of(tier.get("base_url") or ""), "model tier")

        if cfg.get("network_neighbours"):
            for ip, mac in _neighbours():
                if ip in seen:
                    continue
                seen.add(ip)
                nodes.append(_node(f"host:{ip}", ip, "machine", detail=f"seen on the network · {mac}"))
        nodes = _cap(nodes, focus)

    elif focus == "kb" or focus.startswith("dir:"):
        dirs, files = _tree(doc_store)
        if focus == "kb":
            trail.append({"id": "kb", "label": "knowledge"})
            under = _tree_cache.get("under", {})
            roots = _roots(dirs, files)
            # One root is not a choice. Show what is inside it rather than
            # spending a click on a node with nowhere else to go.
            if len(roots) == 1:
                only = roots[0]
                trail += _trail_for(only)[1:]
                children = dirs.get(only, {})
                here_files = sorted(files.get(only, []))
            else:
                children = {d: under.get(d, 0) for d in roots}
                here_files: list[str] = []
        else:
            path = focus[4:]
            trail += _trail_for(path)
            children = dirs.get(path, {})
            here_files = sorted(files.get(path, []))
        nodes = [_node(f"dir:{p}", os.path.basename(p) or p, "folder", count=c)
                 for p, c in sorted(children.items())]
        nodes += [_node(f"file:{p}", os.path.basename(p), "file",
                        ext=os.path.splitext(p)[1].lstrip(".").lower())
                  for p in here_files]
        nodes = _cap(nodes, focus)

    return {
        "focus": focus,
        "trail": trail,
        "nodes": nodes,
        # A star from the focus: the client lays it out, this says what connects.
        "edges": [{"from": focus, "to": n["id"]} for n in nodes],
    }


def _trail_for(path: str) -> list[dict]:
    """Breadcrumb from the knowledge root down to `path`."""
    out = [{"id": "kb", "label": "knowledge"}]
    parts: list[str] = []
    cur = path
    while cur and os.path.dirname(cur) != cur:
        parts.append(cur)
        cur = os.path.dirname(cur)
    if cur:
        parts.append(cur)
    for p in reversed(parts):
        out.append({"id": f"dir:{p}", "label": os.path.basename(p) or p})
    return out


def indexed_paths(doc_store) -> set[str]:
    """Every file the index holds — the allow-list for opening one.

    Opening is limited to this set on purpose: the graph is built from the index,
    so anything it can show, it already holds. Anything else is a path somebody
    typed, and this is not a file browser for the whole disk.
    """
    _, files = _tree(doc_store)
    return {p for group in files.values() for p in group}
