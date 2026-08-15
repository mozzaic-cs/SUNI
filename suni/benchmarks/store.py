"""
Persistence for on-demand benchmark runs.

Each run is one JSON file under memory/benchmarks/. A lightweight index
(index.json) keeps run summaries for the dashboard's history list. Kept separate
from live telemetry (telemetry.py) — these are point-in-time capability
snapshots, not the live workload.
"""
from __future__ import annotations
import json
import os
import tempfile
from datetime import datetime, timezone

_DIR = os.path.join("memory", "benchmarks")
_INDEX = os.path.join(_DIR, "index.json")


def _ensure() -> None:
    os.makedirs(_DIR, exist_ok=True)


def _atomic_write(path: str, obj) -> None:
    _ensure()
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass


def save_run(record: dict) -> str:
    """Persist a full run record; update the index. Returns the file path."""
    _ensure()
    run_id = record["id"]
    path = os.path.join(_DIR, f"{run_id}.json")
    _atomic_write(path, record)

    index = _load_index()
    summary = {
        "id": run_id,
        "started": record.get("started"),
        "finished": record.get("finished"),
        "model": record.get("model"),
        "status": record.get("status"),
        "elapsed_s": record.get("elapsed_s"),
        "suite_keys": list(record.get("suites", {}).keys()),
        "metric_count": len(record.get("metrics", {})),
    }
    index = [s for s in index if s.get("id") != run_id]
    index.insert(0, summary)
    index = index[:100]   # keep last 100
    _atomic_write(_INDEX, index)
    return path


def _load_index() -> list:
    try:
        with open(_INDEX, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def list_runs() -> list:
    """Run summaries, most recent first."""
    return _load_index()


def get_run(run_id: str) -> dict | None:
    path = os.path.join(_DIR, f"{run_id}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def latest() -> dict | None:
    idx = _load_index()
    for s in idx:
        rec = get_run(s["id"])
        if rec and rec.get("status") == "completed":
            return rec
    return None


def latest_metrics() -> dict:
    """
    {metric_id: {"value": v, "run_id":…, "ts":…, "suite":…, "notes":…}} from the
    most recent completed run. Empty if no run yet.
    """
    rec = latest()
    if not rec:
        return {}
    ts = rec.get("finished")
    rid = rec.get("id")
    out: dict = {}
    for skey, sres in rec.get("suites", {}).items():
        notes = sres.get("notes", "")
        for mid, val in (sres.get("metrics") or {}).items():
            out[mid] = {"value": val, "run_id": rid, "ts": ts, "suite": skey, "notes": notes}
    return out


def new_run_id() -> str:
    return "run_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
