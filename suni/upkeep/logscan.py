"""Read SUNI's logs from where the last scan stopped, and pull out the errors.

Two logs, and the second one matters more:

  logs/suni_YYYY-MM-DD.log   SUNI's own logger. Structured, and quiet — most
                             days it holds a dozen lines.
  logs/startup.log           the process's stdout and stderr. Every uncaught
                             traceback, every library's complaint, every
                             print() lands here and nowhere else. diagnostics.py
                             has never read it, and on 2026-11-09 it held 78
                             "dictionary changed size during iteration" errors
                             the daily log never mentioned.

Each error becomes an Event with a fingerprint that survives the details: an
exception is its type plus the deepest frame in SUNI's own code (not the line
number, which moves with every edit); a log line is its tag plus its message
with the numbers, ids and quoted values taken out.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import store

FIRST_WINDOW = 1_000_000      # bytes read from a log the first time it is seen
MAX_READ = 8_000_000          # per log per scan; beyond it the rest is skipped
EVIDENCE = 600                # characters of a single log line kept as evidence


@dataclass
class Event:
    kind: str                  # traceback | log | print
    code: str                  # exception type, or the [TAG]
    title: str
    location: str = ""
    in_suni: bool = False
    evidence: str = ""
    seen_at: str = ""
    frames: list[tuple[str, str]] = field(default_factory=list)   # (path, func)

    @property
    def id(self) -> str:
        if self.kind == "traceback":
            return store.fingerprint("tb", self.code, self.location)
        return store.fingerprint("log", self.code, _normalise(self.title))


# ── known noise: matched by shape, never by "it looks harmless" ──────────────
# Each entry is (exception type, a function that must be among the frames).
# The client-disconnect wart is filtered in logger.py now; it is here because
# startup.log still holds 19,328 of them from before that.
_NOISE = [
    ("ConnectionResetError", "_call_connection_lost"),
    ("ConnectionResetError", "_loop_reading"),
]


def is_noise(ev: Event) -> bool:
    if ev.kind != "traceback":
        return False
    funcs = {f for _, f in ev.frames}
    return any(ev.code.endswith(t) and fn in funcs for t, fn in _NOISE)


# ── normalising a message into something that repeats ────────────────────────

_QUOTED = re.compile(r"""'[^'\n]{0,300}'|"[^"\n]{0,300}"|<[^<>\n]{0,200}>""")
_HEX = re.compile(r"\b(0x)?[0-9a-f]{8,}\b", re.I)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_NUM = re.compile(r"\d+(\.\d+)?")


def _normalise(msg: str) -> str:
    s = _UUID.sub("U", msg)
    s = _QUOTED.sub("Q", s)
    s = _HEX.sub("H", s)
    s = _NUM.sub("N", s)
    return re.sub(r"\s+", " ", s).strip()[:100]


def _scrub(text: str) -> str:
    from ..diagnostics import _scrub as s
    return s(text)


# ── parsing ──────────────────────────────────────────────────────────────────

_STRUCT = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \| (ERROR|CRITICAL)\s*\| (.*)$")
_PRINT = re.compile(r"^([A-Z][A-Za-z]{1,20}(?: [A-Za-z]{1,20})?) error: (.+)$")
_TAG = re.compile(r"^\[([A-Z_]{2,20})\]")
_GROUP_PREFIX = re.compile(r"^\s*(?:[|+]-*\s?)+")
_TB_START = "Traceback (most recent call last):"
_FRAME = re.compile(r'^\s+File "([^"]+)", line \d+, in (\S+)')
_CHAIN = ("During handling of the above exception", "The above exception was the direct cause")


def _location(frames: list[tuple[str, str]]) -> tuple[str, bool]:
    """The deepest frame in SUNI's own code, or else the deepest frame at all."""
    for path, func in reversed(frames):
        p = path.replace("\\", "/")
        if "site-packages" in p or "/lib/python" in p.lower() or "/Lib/" in p:
            continue
        i = p.rfind("/suni/")
        if i >= 0:
            return f"{p[i + 1:]}:{func}", True
    if frames:
        path, func = frames[-1]
        return f"{Path(path).name}:{func}", False
    return "", False


def parse(lines: list[str], source: str, when: str) -> list[Event]:
    """Every error in these lines, tracebacks folded into the line that logged them."""
    events: list[Event] = []
    tb: list[str] | None = None
    frames: list[tuple[str, str]] = []
    attach_to: Event | None = None      # the log line a traceback belongs to
    chain_into: Event | None = None     # "During handling…": same event, later cause
    last_idx = -2

    def finish(exc_line: str) -> None:
        nonlocal tb, frames, attach_to, chain_into
        exc_type = exc_line.split(":", 1)[0].strip()
        loc, mine = _location(frames)
        body = [l for l in (tb or []) if l.strip()]
        sample = "\n".join(body[-8:] if len(body) > 8 else body)
        target = chain_into or attach_to
        if target is not None:
            target.kind, target.code = "traceback", exc_type
            target.frames += frames
            target.location, target.in_suni = _location(target.frames)
            target.evidence = _scrub((target.evidence + "\n" + sample).strip())
            if target is chain_into or not target.title:
                target.title = exc_line.strip()[:200]
            ev = target
        else:
            ev = Event("traceback", exc_type, exc_line.strip()[:200], loc, mine,
                       _scrub(sample), when, list(frames))
            events.append(ev)
        tb, frames, attach_to, chain_into = None, [], None, None
        finish.last = ev

    finish.last = None
    for i, raw in enumerate(lines):
        line = _GROUP_PREFIX.sub("", raw) if raw.lstrip().startswith(("|", "+")) else raw
        if line.startswith("ERROR:") and _TB_START in line:
            line = line[line.index(_TB_START):]
        if tb is not None:
            m = _FRAME.match(line)
            if m:
                frames.append((m.group(1), m.group(2)))
                tb.append(line)
                continue
            if not line.strip() or line[:1].isspace():
                tb.append(line)
                continue
            if _TB_START in line:
                # Another process's traceback cut into this one (multiprocessing
                # children share the stream). Start again on the new one.
                tb, frames = [line], []
                continue
            finish(line)
            continue
        if _TB_START in line:
            tb, frames = [line], []
            prev = events[-1] if events else None
            if prev is not None and last_idx == i - 1 and prev.kind == "log":
                attach_to = prev
            continue
        if any(line.startswith(c) for c in _CHAIN) and finish.last is not None:
            chain_into = finish.last
            continue
        if line.strip():
            chain_into = None           # something else came between: not a chain
        m = _STRUCT.match(line)
        if m:
            msg = m.group(3).strip()
            t = _TAG.match(msg)
            code = t.group(1) if t else "LOG"
            events.append(Event("log", code, _scrub(msg)[:200], "", False,
                                _scrub(line)[:EVIDENCE], m.group(1).replace(" ", "T")))
            last_idx = i
            continue
        m = _PRINT.match(line)
        if m and "Traceback" not in line:
            events.append(Event("print", m.group(1).upper().replace(" ", "_"),
                                _scrub(line.strip())[:200], "", False,
                                _scrub(line.strip())[:EVIDENCE], when))
            last_idx = i
    return [e for e in events if not is_noise(e)]


# ── reading only what is new ─────────────────────────────────────────────────

_HEAD_BYTES = 64


def _head(fh, size: int) -> str:
    """Identifies the file, so a log replaced under the same name is read from
    the top. Empty until the file is long enough to say anything."""
    if size < _HEAD_BYTES:
        return ""
    fh.seek(0)
    return hashlib.sha1(fh.read(_HEAD_BYTES)).hexdigest()[:16]


def read_new(path: Path) -> tuple[list[str], dict]:
    """The complete lines added since the last scan, and what was done to get them."""
    info = {"path": path.name, "read": 0, "skipped": 0, "reset": False, "first": False}
    key = str(path.resolve())
    try:
        size = path.stat().st_size
    except OSError:
        return [], info
    cur = store.cursor(key)
    info["first"] = cur is None
    with path.open("rb") as fh:
        head = _head(fh, size)
        mid_line = False
        if cur is None:
            start = max(0, size - FIRST_WINDOW)
            mid_line = start > 0
        else:
            start, old_head = cur
            if start > size or (old_head and head and old_head != head):
                start, info["reset"] = 0, True     # rotated, truncated or replaced
        if size - start > MAX_READ:
            info["skipped"] = size - start - MAX_READ
            start, mid_line = size - MAX_READ, True
        fh.seek(start)
        data = fh.read(size - start)
    base = start                                 # the file offset of data[0]
    if mid_line:
        nl = data.find(b"\n")                    # begin on a whole line
        if nl < 0:
            store.set_cursor(key, size, head)
            return [], info
        base, data = base + nl + 1, data[nl + 1:]
    end = data.rfind(b"\n")
    if end < 0:                                  # only half a line so far: wait for it
        store.set_cursor(key, base, head)
        return [], info
    complete = data[:end + 1]
    store.set_cursor(key, base + len(complete), head)
    info["read"] = len(complete)
    return complete.decode("utf-8", "replace").splitlines(), info
