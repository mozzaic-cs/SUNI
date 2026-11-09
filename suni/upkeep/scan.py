"""One upkeep scan: diagnostics, then the logs, into the incident book.

Read-only towards everything except memory/upkeep.db. It runs in a worker
thread (diagnostics imports every tool module, and a slow disk read on the
event loop is how the scanner flood once starved every turn), and one scan at
a time: the scheduled one and an admin's "Scan now" share a lock.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import logscan, store

log = logging.getLogger("suni.upkeep")

_lock = threading.Lock()

# Findings that are a state worth tracking. NOTE and OK are not problems, and
# the log-error finding is covered, line by line, by the log scan itself.
_TRACKED_LEVELS = ("broken", "slow")
_SKIP_CODES = {"log.errors"}


def _log_dir() -> Path:
    try:
        from ..logger import _LOG_DIR
        return Path(_LOG_DIR)
    except Exception:      # noqa: BLE001
        return Path("logs")


def log_paths() -> list[Path]:
    d = _log_dir()
    today = datetime.now()
    names = [f"suni_{(today - timedelta(days=1)).strftime('%Y-%m-%d')}.log",
             f"suni_{today.strftime('%Y-%m-%d')}.log",
             "startup.log"]
    return [d / n for n in names if (d / n).exists()]


def _scan_findings() -> tuple[int, set[str]]:
    from .. import diagnostics
    result = diagnostics.run()
    present: set[str] = set()
    for f in result["findings"]:
        if f.level not in _TRACKED_LEVELS or not f.code or f.code in _SKIP_CODES:
            continue
        id = store.fingerprint("finding", f.code)
        present.add(id)
        evidence = "\n".join(x for x in (f.evidence, f.fix and "try: " + f.fix) if x)
        store.record(id=id, kind="finding", code=f.code, level=f.level, title=f.what,
                     evidence=evidence, source="diagnostics", initial_state="watching")
    store.promote_watching(present)
    store.findings_seen(present)
    return len(result["findings"]), present


def _scan_logs(first_scan: bool) -> tuple[int, list[dict]]:
    total, reads = 0, []
    when = store.now()
    for path in log_paths():
        lines, info = logscan.read_new(path)
        reads.append(info)
        if not lines:
            continue
        # Count first, record once per distinct problem: a burst of 300 is one
        # write with count=300, not 300 writes.
        grouped: dict[str, tuple[logscan.Event, int]] = {}
        for ev in logscan.parse(lines, path.name, when):
            prev = grouped.get(ev.id)
            grouped[ev.id] = (ev, (prev[1] if prev else 0) + 1)
        # What the logs held before upkeep ever ran is history: startup.log
        # has no timestamps, and a month-old error is not news. It goes in the
        # book as resolved, and re-opens the next time it actually happens.
        # Only on upkeep's FIRST scan — every day brings a new suni_*.log, and
        # what it holds by the first scan after midnight is today's news.
        history = first_scan and info["first"]
        for id, (ev, n) in grouped.items():
            store.record(id=id, kind="error", code=ev.code, level="error", title=ev.title,
                         location=ev.location, in_suni=ev.in_suni, evidence=ev.evidence,
                         source=path.name, seen_at=ev.seen_at or when, count=n,
                         history=history)
            total += n
    return total, reads


def scan() -> dict:
    """Run one scan now. Returns what it did, which is also kept as last_scan."""
    if not _lock.acquire(blocking=False):
        return {"busy": True, **(store.get_meta("last_scan", {}) or {})}
    t0 = time.perf_counter()
    out: dict = {"at": store.now()}
    first_scan = store.get_meta("watching_since") is None
    try:
        try:
            out["findings_checked"], _ = _scan_findings()
        except Exception as exc:      # noqa: BLE001 — one half failing is not the scan failing
            log.warning("[UPKEEP] diagnostics part failed: %s", exc)
            out["findings_error"] = str(exc)[:200]
        try:
            out["errors_seen"], out["logs"] = _scan_logs(first_scan)
            if first_scan:
                store.set_meta("watching_since", out["at"])
        except Exception as exc:      # noqa: BLE001
            log.warning("[UPKEEP] log part failed: %s", exc)
            out["logs_error"] = str(exc)[:200]
        out["resolved_quiet"] = store.resolve_quiet_errors()
        out["took_s"] = round(time.perf_counter() - t0, 2)
        store.set_meta("last_scan", out)
        return out
    finally:
        _lock.release()
