"""
Noticing when SUNI is alive but has stopped listening.

There is a failure mode worse than crashing: the process keeps running, keeps
logging, keeps scanning documents and keeps its heartbeat current — and answers
nobody, because its listening socket is gone. Reproduced on 01/09/2026 with a
burst of abruptly-reset connections: Windows raised

    OSError: [WinError 64] The specified network name is no longer available

from the proactor accept loop (`proactor_events.py`, in `loop`), the listener
died, and asyncio did not bring it back. The process stayed up at 941 MB happily
indexing files, owning no listening socket at all.

Every signal SUNI had said it was healthy. `runstate`'s heartbeat proved only
that the scheduler loop still turned, and it turned fine. That is the gap this
closes.

**It does not repair the listener; it ends the process so a supervisor restarts
it.** Rebuilding a socket underneath a running uvicorn is far more delicate than
starting again, and both supported deployments now restart on their own — the
Windows scheduled task on a ten-minute trigger, systemd on Linux. Crash-only is
the honest design once something reliable is watching.

TWO SAFEGUARDS, BECAUSE A BAD HEALTH CHECK IS AN OUTAGE GENERATOR
A watchdog that kills a healthy server on a bad reading is worse than no
watchdog at all, so:

  1. **It must see the listener work before it will act on failure.** If the
     very first probes fail, the probe itself is what is broken — loopback
     blocked by security software, an unusual network stack — and the watchdog
     disables itself with a warning instead of restart-looping the machine
     forever. Failure is only trusted from something that has demonstrably
     succeeded here.

  2. **Consecutive failures, never one.** A single probe can lose to load or a
     slow accept. Three ticks of the scheduler loop is a minute and a half of
     unbroken silence, which no healthy server produces.
"""
from __future__ import annotations

import socket

from .logger import get_logger

_log = get_logger(__name__)

# Three consecutive misses on the 30s scheduler tick ≈ 90s of a server that
# accepts nothing. Under one, ordinary load could trip it.
FAILURES_BEFORE_EXIT = 3


def probe(port: int, host: str = "127.0.0.1", timeout: float = 5.0) -> bool:
    """Can anything still connect to us?

    A plain TCP connect, deliberately: it answers exactly the question asked —
    is the accept loop alive — without a TLS handshake, a certificate, or a
    route through the application. Closed politely (FIN, not RST) so the check
    does not manufacture the very disconnect noise it might be diagnosing.
    """
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class ListenerWatchdog:
    """Turns a stream of probe results into one of: ok, warn, exit, disabled."""

    def __init__(self, failures_before_exit: int = FAILURES_BEFORE_EXIT):
        self.failures_before_exit = failures_before_exit
        self.ever_succeeded = False
        self.consecutive_failures = 0
        self.disabled = False

    def check(self, alive: bool) -> str:
        """Feed one probe result in; get the action out.

        Returns "ok", "warn" (failing but not yet at the limit), "exit" (the
        caller should end the process for a supervisor to restart), or
        "disabled" (the probe never worked here and is not to be trusted).
        """
        if self.disabled:
            return "disabled"

        if alive:
            self.ever_succeeded = True
            self.consecutive_failures = 0
            return "ok"

        if not self.ever_succeeded:
            # Never once saw the listener. The probe is the broken thing.
            self.disabled = True
            _log.warning(
                "[WATCHDOG] the listener probe has never succeeded — disabling "
                "it rather than trusting its failures. SUNI keeps running; it "
                "just will not notice if it stops accepting connections.")
            return "disabled"

        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failures_before_exit:
            return "exit"
        _log.warning("[WATCHDOG] listener did not answer (%d/%d)",
                     self.consecutive_failures, self.failures_before_exit)
        return "warn"
