"""
Compose the full 33-metric dashboard payload: static definitions + current
values from the three sources (live telemetry, estimates, last benchmark run).

Every metric gets an explicit `status` so the UI never shows a fabricated
number:
  live         value from real-inference telemetry
  estimated    derived from config/hardware, approximate
  measured     from the most recent benchmark run (with timestamp)
  awaiting     source exists but no data yet (e.g. no requests / no run)
  na           no trustworthy local measurement (shows reason, never a number)
"""
from __future__ import annotations

from . import telemetry
from . import estimates as _est
from . import store as _store
from .metrics import METRICS, CATEGORY_ORDER, LIVE, ON_DEMAND, ESTIMATED, NA

# telemetry live_metrics()/agent_metrics() key -> metric id
_LIVE_MAP = {
    "ttft": "ttft",
    "tpot": "tpot",
    "tps": "tps",
    "throughput": "throughput",
    "error_rate": "error_rate",
    "token_efficiency": "token_efficiency",
    "tail_latency_p95": "tail_latency",
    "plan_stability": "plan_stability",
    "self_correction": "self_correction",
}


async def build_payload(model: str, host: str, num_ctx: int,
                        kwh_price: float | None = None) -> dict:
    live = telemetry.live_metrics()
    agent = telemetry.agent_metrics()
    est = await _est.estimate(model, host, live, kwh_price)
    ondemand = _store.latest_metrics()
    last_run = _store.latest()

    live_all = {**live, **agent}

    def _entry(m) -> dict:
        d = m.to_dict()
        value, status, detail, ts = None, None, "", None

        if m.source == LIVE:
            # find the telemetry key that maps to this metric id
            src_key = next((k for k, v in _LIVE_MAP.items() if v == m.id), m.id)
            value = live_all.get(src_key)
            status = "live" if value is not None else "awaiting"
            if m.id == "tail_latency" and live.get("tail_latency_p99") is not None:
                detail = f"p99 {live['tail_latency_p99']} ms"
            if status == "awaiting":
                detail = detail or "no requests recorded yet"

        elif m.source == ESTIMATED:
            e = est.get(m.id) or {}
            value = e.get("value")
            detail = e.get("detail", "")
            status = "estimated" if value is not None else "awaiting"

        elif m.source == ON_DEMAND:
            entry = ondemand.get(m.id)
            if entry and entry.get("value") is not None:
                value = entry["value"]
                ts = entry.get("ts")
                detail = entry.get("notes", "")
                status = "measured"
            else:
                status = "awaiting"
                detail = "not benchmarked yet - run the suite"

        elif m.source == NA:
            status = "na"
            detail = m.na_reason

        d.update({"value": value, "status": status, "detail": detail, "ts": ts})
        return d

    groups = {c: [] for c in CATEGORY_ORDER}
    for m in METRICS:
        groups.setdefault(m.category, []).append(_entry(m))

    categories = [{"category": c, "metrics": groups[c]}
                  for c in CATEGORY_ORDER if groups.get(c)]

    # summary counts by status
    from collections import Counter
    counts = Counter(mm["status"] for g in categories for mm in g["metrics"])

    return {
        "categories": categories,
        "summary": {
            "total": len(METRICS),
            "by_status": dict(counts),
            "sample_count": live.get("sample_count", 0),
            "task_count": agent.get("task_count", 0),
        },
        "model": model,
        "num_ctx": num_ctx,
        "last_run": {
            "id": last_run.get("id"),
            "finished": last_run.get("finished"),
            "elapsed_s": last_run.get("elapsed_s"),
            "suite_keys": list(last_run.get("suites", {}).keys()),
        } if last_run else None,
    }
