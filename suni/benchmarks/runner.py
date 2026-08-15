"""
On-demand benchmark runner.

Runs the selected objective suites against the configured Ollama model, one at a
time (there is a single Ollama instance — parallel runs would thrash it and
corrupt timings). Admin-triggered only. While a run is in progress SUNI's live
assistant shares the GPU and will be slower, so the API guards against
concurrent runs and the UI warns the user.

Generation params are pinned (temperature/seed) by each suite so results are
reproducible; the runner just provides the `gen` callable and records timing.
"""
from __future__ import annotations
import asyncio
import time
from datetime import datetime, timezone

from ..logger import get_logger
from . import suites as _suites
from . import store as _store
from .metrics import all_suite_keys

_log = get_logger(__name__)


class _Progress:
    """Thread-safe-ish progress snapshot for the API to poll."""
    def __init__(self) -> None:
        self.running = False
        self.run_id: str | None = None
        self.suite: str | None = None
        self.suite_index = 0
        self.suite_total = 0
        self.item = 0
        self.item_total = 0
        self.started: str | None = None
        self.error: str | None = None
        self.done_suites: list[str] = []

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "run_id": self.run_id,
            "suite": self.suite,
            "suite_index": self.suite_index,
            "suite_total": self.suite_total,
            "item": self.item,
            "item_total": self.item_total,
            "started": self.started,
            "error": self.error,
            "done_suites": list(self.done_suites),
        }


# Module-level singletons: one run at a time.
_PROGRESS = _Progress()
_LOCK = asyncio.Lock()


def is_running() -> bool:
    return _PROGRESS.running


def progress() -> dict:
    return _PROGRESS.snapshot()


def _make_gen(model: str, host: str, num_ctx: int):
    import ollama
    from .. import config as _c
    client = ollama.AsyncClient(host=host or _c.ollama_host())

    async def gen(prompt, *, system=None, temperature=0.0, seed=0,
                  num_predict=512, tools=None, format=None):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        options = {
            "num_ctx": num_ctx,
            "temperature": temperature,
            "seed": seed,
            "num_predict": num_predict,
        }
        kwargs = {"model": model, "messages": messages, "options": options, "keep_alive": -1}
        if tools:
            kwargs["tools"] = tools
        if format:
            kwargs["format"] = format
        try:
            resp = await client.chat(**kwargs)
            msg = resp.message
            tool_calls = []
            for tc in (msg.tool_calls or []):
                tool_calls.append({
                    "name": tc.function.name,
                    "args": dict(tc.function.arguments) if tc.function.arguments else {},
                })
            return {"text": msg.content or "", "tool_calls": tool_calls, "error": None}
        except Exception as e:
            return {"text": "", "tool_calls": [], "error": str(e)}

    return gen


async def run(
    *,
    model: str,
    host: str = "",
    num_ctx: int = 8192,
    suite_keys: list[str] | None = None,
    limit: int | None = None,
    registry_tools: list[dict] | None = None,
) -> dict:
    """
    Execute the selected suites and persist a run record. Raises RuntimeError if
    a run is already in progress.
    """
    if _PROGRESS.running:
        raise RuntimeError("A benchmark run is already in progress.")

    keys = suite_keys or all_suite_keys()
    keys = [k for k in keys if _suites.get(k)]

    async with _LOCK:
        _PROGRESS.__init__()
        _PROGRESS.running = True
        run_id = _store.new_run_id()
        _PROGRESS.run_id = run_id
        _PROGRESS.started = datetime.now(timezone.utc).isoformat()
        _PROGRESS.suite_total = len(keys)
        t0 = time.perf_counter()

        gen = _make_gen(model, host, num_ctx)

        def _pcb(suite_key, item, item_total):
            _PROGRESS.item = item
            _PROGRESS.item_total = item_total

        suites_out: dict = {}
        metrics_flat: dict = {}
        status = "completed"
        try:
            for i, key in enumerate(keys):
                _PROGRESS.suite = key
                _PROGRESS.suite_index = i + 1
                _PROGRESS.item = 0
                _PROGRESS.item_total = 0
                ctx = {
                    "limit": limit,
                    "progress": _pcb,
                    "num_ctx": num_ctx,
                    "registry_tools": registry_tools or [],
                }
                fn = _suites.get(key)
                _log.info("[BENCH] running suite %s (%d/%d)", key, i + 1, len(keys))
                try:
                    res = await fn(gen, ctx)
                    rd = res.to_dict()
                except Exception as e:
                    _log.warning("[BENCH] suite %s failed: %s", key, e)
                    rd = {"suite": key, "metrics": {}, "n": 0, "error": str(e),
                          "notes": "suite raised an exception"}
                suites_out[key] = rd
                for mid, val in (rd.get("metrics") or {}).items():
                    metrics_flat[mid] = val
                _PROGRESS.done_suites.append(key)
        except Exception as e:
            status = "error"
            _PROGRESS.error = str(e)
            _log.error("[BENCH] run aborted: %s", e)
        finally:
            elapsed = round(time.perf_counter() - t0, 1)
            record = {
                "id": run_id,
                "started": _PROGRESS.started,
                "finished": datetime.now(timezone.utc).isoformat(),
                "elapsed_s": elapsed,
                "model": model,
                "num_ctx": num_ctx,
                "limit": limit,
                "status": status,
                "suites": suites_out,
                "metrics": metrics_flat,
            }
            try:
                _store.save_run(record)
            except Exception as e:
                _log.error("[BENCH] failed to persist run: %s", e)
            _PROGRESS.running = False
            _log.info("[BENCH] run %s %s in %.1fs (%d metrics)",
                      run_id, status, elapsed, len(metrics_flat))
        return record
