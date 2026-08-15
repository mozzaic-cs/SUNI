"""
Backend health / circuit breaker for SUNI's local (Ollama) inference path.

When Ollama goes down, every request would otherwise hang on a connection
timeout and thrash through tier escalation before failing. This breaker turns
that into a fast, clear failure and auto-recovers when the backend returns.

Design (advisor-reviewed):
  • FAILURE CLASSIFICATION is the whole game. Only CONNECTION-level failures
    (backend is down: ConnectionError / timeouts) move the breaker. REQUEST-level
    failures (the backend answered but this request failed: context overflow, bad
    tool schema, model-not-found → ollama.ResponseError) must NEVER trip it, or
    one bad prompt would block a healthy backend for everyone.
  • IN-BAND OPEN, BACKGROUND-PROBE CLOSE. The Nth consecutive connection failure
    opens the breaker immediately on real traffic. A background probe (liveness
    only — client.list(), never inference) owns OPEN→CLOSED recovery. No
    HALF_OPEN gate, so there is no request-admission race to get right.
  • Per-process state (single worker — same basis as the OIDC state store).
  • Default ON; `backend_breaker` config is the kill switch. Safe to default on
    precisely because classification keeps a healthy backend untouched.
"""
from __future__ import annotations
import asyncio
import logging
import threading
import time

import httpx

from .. import config as _cfg

log = logging.getLogger("suni.health")

_DEFAULT_THRESHOLD = 3     # consecutive connection failures before opening
_DEFAULT_INTERVAL  = 30    # seconds between recovery probes
_PROBE_TIMEOUT     = 5     # seconds for a liveness probe


class BackendUnavailableError(Exception):
    """Raised in place of a doomed inference call while the breaker is OPEN."""


def enabled() -> bool:
    return bool(_cfg.get("backend_breaker", True))


def _threshold() -> int:
    try:
        return max(1, int(_cfg.get("backend_breaker_threshold", _DEFAULT_THRESHOLD)))
    except Exception:
        return _DEFAULT_THRESHOLD


# ── Failure classification ────────────────────────────────────────────────────
# Connection-level exceptions the ollama async client surfaces when the backend
# is unreachable. Verified empirically: a refused connection arrives as builtin
# ConnectionError; timeouts as httpx/asyncio timeout types. ollama.ResponseError
# (a request-level error) is deliberately NOT here.
_CONNECTION_EXC: tuple = (
    ConnectionError,          # builtin — ollama re-raises refused connections as this
    TimeoutError,             # builtin (== asyncio.TimeoutError on 3.11+)
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.PoolTimeout,
    httpx.TimeoutException,
)
# The openai client (vLLM path) wraps httpx errors in its own types, so the httpx
# classes above never surface for vLLM — add the openai connection types.
# openai.BadRequestError (context-length/400) is deliberately NOT included: it is
# request-level, the analog of ollama.ResponseError.
try:
    import openai as _openai
    _CONNECTION_EXC = _CONNECTION_EXC + (_openai.APIConnectionError, _openai.APITimeoutError)
except Exception:
    pass


def is_connection_failure(exc: BaseException) -> bool:
    """True only for 'backend is down/unreachable' errors — the ONLY thing that
    may move the breaker. Everything else (incl. ollama.ResponseError and
    openai.BadRequestError) is request-level and returns False."""
    return isinstance(exc, _CONNECTION_EXC)


# ── Breaker state (per host) ──────────────────────────────────────────────────

class _Breaker:
    __slots__ = ("fails", "open", "opened_at")

    def __init__(self) -> None:
        self.fails = 0
        self.open = False
        self.opened_at = 0.0


_lock: threading.Lock = threading.Lock()
_breakers: dict[str, _Breaker] = {}


def _get(host: str) -> _Breaker:
    with _lock:
        b = _breakers.get(host)
        if b is None:
            b = _Breaker()
            _breakers[host] = b
        return b


def allow(host: str) -> bool:
    """False while the breaker for `host` is OPEN — caller should fast-fail."""
    return not _get(host).open


def record_success(host: str) -> None:
    b = _get(host)
    with _lock:
        was_open = b.open
        b.fails = 0
        b.open = False
    if was_open:
        log.info("[BACKEND] %s recovered → breaker CLOSED", host)


def record_failure(host: str) -> None:
    """Call ONLY for connection-level failures (see is_connection_failure)."""
    b = _get(host)
    thr = _threshold()
    with _lock:
        b.fails += 1
        if not b.open and b.fails >= thr:
            b.open = True
            b.opened_at = time.monotonic()
            newly_open = True
        else:
            newly_open = False
    if newly_open:
        log.warning("[BACKEND] %s: %d consecutive connection failures → breaker OPEN", host, thr)


def status() -> dict:
    with _lock:
        now = time.monotonic()
        return {
            "enabled": enabled(),
            "hosts": {
                h: {
                    "state": "open" if b.open else "closed",
                    "consecutive_failures": b.fails,
                    "open_for_s": round(now - b.opened_at, 1) if b.open else 0,
                }
                for h, b in _breakers.items()
            },
        }


# ── Background recovery probe ─────────────────────────────────────────────────

async def _probe(host: str) -> bool:
    """Liveness only — is the server up? Never loads a model / runs inference.
    vLLM hosts (base_url ending in /v1) are probed via GET /v1/models; Ollama
    hosts via the native client.list()."""
    try:
        if "/v1" in host:
            url = host.rstrip("/") + "/models"
            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT, verify=True) as c:
                await c.get(url)
                return True   # any HTTP response (incl. 401/403) means the server is up
        import ollama
        client = ollama.AsyncClient(host=host)
        await asyncio.wait_for(client.list(), timeout=_PROBE_TIMEOUT)
        return True
    except Exception:
        return False


async def start_monitor(stop_event: asyncio.Event) -> None:
    """Probe OPEN hosts every interval; close the breaker when one answers.
    Only OPEN breakers are probed — healthy hosts are maintained by real traffic."""
    log.info("[BACKEND] health monitor started")
    while not stop_event.is_set():
        try:
            interval = max(5, int(_cfg.get("backend_probe_interval_s", _DEFAULT_INTERVAL)))
        except Exception:
            interval = _DEFAULT_INTERVAL
        if enabled():
            with _lock:
                open_hosts = [h for h, b in _breakers.items() if b.open]
            for host in open_hosts:
                if await _probe(host):
                    record_success(host)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("[BACKEND] health monitor stopped")
