"""
Passive telemetry — the *live* half of the benchmark system.

Every real inference records one Sample here (from ollama_agent.chat). Nothing
in this module ever calls the model; it only observes work SUNI already does,
so it is free and reflects the true workload rather than a synthetic prompt.

The `live_metrics()` snapshot feeds the dashboard's live column: TTFT, tok/s,
tail latency, error rate, throughput, token efficiency. Values are computed
over a bounded ring buffer so memory stays flat.

Kept deliberately separate from on-demand benchmark scores (see runner.py):
tok/s measured over varied real prompts is a different number from tok/s on a
fixed benchmark prompt, and conflating them makes both meaningless.
"""
from __future__ import annotations
import threading
import time
from collections import deque
from dataclasses import dataclass

# Ring-buffer capacity. ~2000 recent inferences is plenty for stable
# percentiles while staying tiny in memory.
_MAXLEN = 2000

# Agent-event buffers (tool calls / planning rounds) for the two live
# Agent-Behaviour metrics. Smaller — these are per-tool, not per-token.
_AGENT_MAXLEN = 4000

# Trailing window (seconds) for rate-style metrics (throughput, error rate).
_WINDOW_S = 300.0


@dataclass
class Sample:
    ts: float               # time.time() at completion
    ok: bool                # False if the inference raised / refused
    latency_ms: float       # wall-clock for the whole chat() call
    ttft_ms: float | None   # load_duration + prompt_eval_duration proxy
    prompt_tok: int | None
    gen_tok: int | None
    tps: float | None       # gen_tok / eval_duration
    tpot_ms: float | None   # eval_duration / gen_tok


@dataclass
class ToolEvent:
    ts: float
    task: str      # grouping key (id(context)) — ties events within one task
    name: str
    ok: bool


@dataclass
class RoundEvent:
    ts: float
    task: str      # one per tool-execution round; >1 round = the agent re-planned


class _Telemetry:
    def __init__(self) -> None:
        self._samples: deque[Sample] = deque(maxlen=_MAXLEN)
        self._tools: deque[ToolEvent] = deque(maxlen=_AGENT_MAXLEN)
        self._rounds: deque[RoundEvent] = deque(maxlen=_AGENT_MAXLEN)
        self._lock = threading.Lock()

    # ── recording ────────────────────────────────────────────────────────────
    def record(
        self,
        *,
        ok: bool,
        latency_ms: float,
        load_ns: int | None = None,
        prompt_eval_ns: int | None = None,
        eval_ns: int | None = None,
        prompt_tok: int | None = None,
        gen_tok: int | None = None,
    ) -> None:
        """Record one inference. Safe to call from any thread; never raises."""
        try:
            # TTFT proxy: time before the first output token is produced ≈
            # model load (0 when warm) + prompt evaluation. Ollama returns both
            # in nanoseconds; we avoid a streaming refactor of the hot path.
            ttft_ms = None
            if prompt_eval_ns is not None:
                ttft_ms = (prompt_eval_ns + (load_ns or 0)) / 1e6

            tps = tpot_ms = None
            if gen_tok and eval_ns and eval_ns > 0:
                tps = gen_tok / (eval_ns / 1e9)
                tpot_ms = (eval_ns / 1e6) / gen_tok

            s = Sample(
                ts=time.time(), ok=ok, latency_ms=latency_ms, ttft_ms=ttft_ms,
                prompt_tok=prompt_tok, gen_tok=gen_tok, tps=tps, tpot_ms=tpot_ms,
            )
            with self._lock:
                self._samples.append(s)
        except Exception:
            pass  # telemetry must never break inference

    def record_tool(self, task: str, name: str, ok: bool) -> None:
        try:
            with self._lock:
                self._tools.append(ToolEvent(ts=time.time(), task=task, name=name, ok=ok))
        except Exception:
            pass

    def record_round(self, task: str) -> None:
        try:
            with self._lock:
                self._rounds.append(RoundEvent(ts=time.time(), task=task))
        except Exception:
            pass

    # ── reading ──────────────────────────────────────────────────────────────
    def _recent(self) -> list[Sample]:
        with self._lock:
            return list(self._samples)

    def agent_metrics(self) -> dict:
        """
        Derive the two live Agent-Behaviour metrics from real task activity:

          self_correction  % of tool failures that were recovered — a failed
                           tool followed later, within the same task, by a
                           success of that same tool.
          plan_stability   average tool-execution rounds per task. One round is
                           a single plan; extra rounds mean the agent re-planned,
                           so higher = less stable.
        """
        with self._lock:
            tools = list(self._tools)
            rounds = list(self._rounds)

        out: dict = {}

        # self-correction — group tool events by task, in order
        by_task: dict[str, list[ToolEvent]] = {}
        for e in tools:
            by_task.setdefault(e.task, []).append(e)
        failures = recovered = 0
        for evs in by_task.values():
            failed_tools: set[str] = set()
            for e in evs:
                if not e.ok:
                    failed_tools.add(e.name)
                    failures += 1
                elif e.name in failed_tools:
                    recovered += 1
                    failed_tools.discard(e.name)
        out["self_correction"] = round(100.0 * recovered / failures, 1) if failures else None
        out["tool_failures"] = failures

        # plan stability — rounds per task
        rounds_per_task: dict[str, int] = {}
        for r in rounds:
            rounds_per_task[r.task] = rounds_per_task.get(r.task, 0) + 1
        if rounds_per_task:
            out["plan_stability"] = round(sum(rounds_per_task.values()) / len(rounds_per_task), 2)
            out["task_count"] = len(rounds_per_task)
        else:
            out["plan_stability"] = None
            out["task_count"] = 0
        return out

    def live_metrics(self) -> dict:
        """
        Snapshot keyed by metric id (matches metrics.py). Values are None when
        there is not yet any data, so the UI shows "—" rather than 0.
        """
        s = self._recent()
        n = len(s)
        if n == 0:
            return {"sample_count": 0}

        now = time.time()
        ok = [x for x in s if x.ok]
        window = [x for x in s if now - x.ts <= _WINDOW_S]
        window_ok = [x for x in window if x.ok]

        def _mean(vals):
            vals = [v for v in vals if v is not None]
            return sum(vals) / len(vals) if vals else None

        def _pct(vals, p):
            vals = sorted(v for v in vals if v is not None)
            if not vals:
                return None
            k = max(0, min(len(vals) - 1, int(round((p / 100.0) * (len(vals) - 1)))))
            return vals[k]

        latencies = [x.latency_ms for x in ok]

        # throughput: completed requests in the trailing window, per minute
        throughput = round(len(window) / (_WINDOW_S / 60.0), 2) if window else 0.0

        # error rate over the window (fall back to all-time if window empty)
        err_pool = window if window else s
        err_rate = round(100.0 * sum(1 for x in err_pool if not x.ok) / len(err_pool), 2)

        # token efficiency: gen / (prompt+gen)
        eff = []
        for x in ok:
            if x.gen_tok and x.prompt_tok is not None:
                total = x.gen_tok + x.prompt_tok
                if total > 0:
                    eff.append(x.gen_tok / total)

        return {
            "sample_count": n,
            "ok_count": len(ok),
            "window_count": len(window),
            "ttft": _round(_mean([x.ttft_ms for x in ok])),
            "tpot": _round(_mean([x.tpot_ms for x in ok])),
            "tps": _round(_mean([x.tps for x in ok])),
            "throughput": throughput,
            "error_rate": err_rate,
            "token_efficiency": _round(_mean(eff), 3),
            "tail_latency_p95": _round(_pct(latencies, 95)),
            "tail_latency_p99": _round(_pct(latencies, 99)),
            "latency_mean": _round(_mean(latencies)),
            "gen_tok_mean": _round(_mean([x.gen_tok for x in ok])),
            "prompt_tok_mean": _round(_mean([x.prompt_tok for x in ok])),
        }

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._tools.clear()
            self._rounds.clear()


def _round(v, ndigits: int = 1):
    return round(v, ndigits) if isinstance(v, (int, float)) else v


# Process-wide singleton.
_TELEMETRY = _Telemetry()


def record(**kw) -> None:
    _TELEMETRY.record(**kw)


def record_tool(task: str, name: str, ok: bool) -> None:
    _TELEMETRY.record_tool(task, name, ok)


def record_round(task: str) -> None:
    _TELEMETRY.record_round(task)


def live_metrics() -> dict:
    return _TELEMETRY.live_metrics()


def agent_metrics() -> dict:
    return _TELEMETRY.agent_metrics()


def reset() -> None:
    _TELEMETRY.reset()
