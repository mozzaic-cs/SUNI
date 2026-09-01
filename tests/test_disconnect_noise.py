"""
The client-disconnect noise filter — does it silence the right thing, and only
the right thing?

A filter is a liability the moment it swallows one real error, so the negative
case here matters more than the positive one: a ConnectionResetError raised
anywhere OTHER than a socket teardown must still reach the handler. The
suppressed shape is counted rather than discarded, which is what these assert on.

Background: an idle browser tab on the Face page recycles keep-alive sockets
every few minutes, and Windows raises ConnectionResetError from
_call_connection_lost for each one, with a six-line traceback. logs/startup.log
reached 19,328 of them — 97% of the file — and they are why the 28 Aug outage
left nothing findable.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from suni import logger as _logger


@pytest.fixture(autouse=True)
def _reset_counter():
    _logger._disconnect_noise = 0
    yield
    _logger._disconnect_noise = 0


class _FakeHandle:
    """Stands in for asyncio's Handle, whose repr names the callback."""
    def __init__(self, text: str):
        self._text = text

    def __str__(self) -> str:
        return self._text


_TEARDOWN = _FakeHandle(
    "<Handle _ProactorBasePipeTransport._call_connection_lost(None)>")


# ── what must be silenced ────────────────────────────────────────────────────

def test_the_windows_disconnect_traceback_is_recognised():
    ctx = {"exception": ConnectionResetError(10054, "forcibly closed"),
           "handle": _TEARDOWN}
    assert _logger._is_client_disconnect(ctx) is True


def test_an_aborted_connection_counts_too():
    ctx = {"exception": ConnectionAbortedError(10053, "aborted"),
           "handle": _TEARDOWN}
    assert _logger._is_client_disconnect(ctx) is True


# ── what must NOT be silenced (the point of the whole thing) ────────────────

def test_a_reset_from_anywhere_else_is_not_noise():
    """The exception TYPE alone must never be the test. A reset raised by, say,
    an outbound call to Ollama is a real event about a real dependency."""
    ctx = {"exception": ConnectionResetError(10054, "forcibly closed"),
           "handle": _FakeHandle("<Handle Orchestrator._call_ollama()>")}
    assert _logger._is_client_disconnect(ctx) is False


def test_an_unrelated_error_on_the_teardown_path_is_not_noise():
    """Same callback, different failure — a bug in teardown must still surface."""
    ctx = {"exception": ValueError("something genuinely wrong"),
           "handle": _TEARDOWN}
    assert _logger._is_client_disconnect(ctx) is False


def test_a_context_with_no_exception_is_not_noise():
    assert _logger._is_client_disconnect({"message": "loop stalled"}) is False


# ── end to end on a real event loop ─────────────────────────────────────────

async def test_the_filter_suppresses_and_counts_on_a_real_loop(caplog):
    assert _logger.install_disconnect_noise_filter() is True
    handler = asyncio.get_running_loop().get_exception_handler()

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            handler(asyncio.get_running_loop(),
                    {"exception": ConnectionResetError(10054, "closed"),
                     "handle": _TEARDOWN,
                     "message": "Exception in callback"})

    assert _logger.suppressed_disconnect_count() == 3, "not counted"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], \
        "the noise still reached the log"


async def test_a_real_error_still_reaches_the_previous_handler():
    """The negative control. Chained, not replaced — anything else that wanted
    loop exceptions must still get them."""
    seen: list[dict] = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda l, ctx: seen.append(ctx))

    assert _logger.install_disconnect_noise_filter() is True
    handler = loop.get_exception_handler()

    handler(loop, {"exception": ConnectionResetError(10054, "closed"),
                   "handle": _TEARDOWN})                      # noise
    handler(loop, {"exception": ValueError("a real fault"),
                   "handle": _FakeHandle("<Handle something_real()>")})

    assert len(seen) == 1, f"expected exactly the real error, got {len(seen)}"
    assert isinstance(seen[0]["exception"], ValueError)
    assert _logger.suppressed_disconnect_count() == 1


async def test_the_rate_is_still_reported_periodically(caplog):
    """Suppressed, not discarded: a client stuck in a reconnect loop must still
    be visible as a rate, which the flood of tracebacks actually obscured."""
    _logger.install_disconnect_noise_filter()
    handler = asyncio.get_running_loop().get_exception_handler()

    with caplog.at_level(logging.INFO):
        for _ in range(_logger._NOISE_REPORT_EVERY):
            handler(asyncio.get_running_loop(),
                    {"exception": ConnectionResetError(10054, "closed"),
                     "handle": _TEARDOWN})

    assert any("client disconnects filtered" in r.message for r in caplog.records), \
        "a sustained disconnect rate would now be completely invisible"


def test_it_reports_failure_when_there_is_no_loop():
    """Called off-loop (a CLI entrypoint), it must decline rather than raise."""
    assert _logger.install_disconnect_noise_filter() is False
