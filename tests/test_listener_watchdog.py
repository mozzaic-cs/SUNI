"""
The listener watchdog — will it catch a deaf server, and will it leave a healthy
one alone?

The second question is the important one. This thing ends the process, so a
false positive is not a bad diagnostic, it is a self-inflicted outage, and on a
supervised host a restart loop. The tests are weighted accordingly: most of them
are about NOT firing.

The failure it exists for was reproduced on 01/09/2026 — a burst of abruptly
reset connections killed the proactor accept loop with WinError 64, and the
process carried on at 941 MB, logging happily, indexing documents, owning no
listening socket. Every other signal called that healthy, including the
runstate heartbeat, because the scheduler loop really was still turning.
"""
from __future__ import annotations

import socket
import threading

import pytest

from suni.listener_watchdog import ListenerWatchdog, probe


# ── the thing it exists to catch ─────────────────────────────────────────────

def test_it_fires_after_enough_consecutive_silence():
    w = ListenerWatchdog(failures_before_exit=3)
    assert w.check(True) == "ok"          # prove the probe works here first
    assert w.check(False) == "warn"
    assert w.check(False) == "warn"
    assert w.check(False) == "exit"


def test_the_streak_resets_on_any_success():
    """Two misses and a recovery is a blip, not a dead listener."""
    w = ListenerWatchdog(failures_before_exit=3)
    w.check(True)
    w.check(False); w.check(False)
    assert w.check(True) == "ok"
    assert w.consecutive_failures == 0
    assert w.check(False) == "warn", "the streak carried over a success"


# ── the far more important half: NOT firing ─────────────────────────────────

def test_one_bad_reading_never_kills_the_server():
    """A single probe can lose to load or a slow accept."""
    w = ListenerWatchdog(failures_before_exit=3)
    w.check(True)
    assert w.check(False) == "warn"


def test_it_refuses_to_act_on_a_probe_that_has_never_worked(caplog):
    """If the FIRST probes fail, the probe is what is broken — loopback blocked
    by security software, an odd network stack. Trusting it would restart-loop
    the host forever, which is worse than the fault it looks for."""
    w = ListenerWatchdog(failures_before_exit=3)
    for _ in range(10):
        assert w.check(False) == "disabled"
    assert w.disabled is True


def test_once_disabled_it_stays_disabled_even_if_a_probe_later_works():
    """Ambiguous evidence must not re-arm something that kills the process."""
    w = ListenerWatchdog(failures_before_exit=3)
    w.check(False)
    assert w.disabled is True
    assert w.check(True) == "disabled"
    assert w.check(False) == "disabled"


def test_a_healthy_server_is_never_touched():
    w = ListenerWatchdog(failures_before_exit=3)
    for _ in range(200):
        assert w.check(True) == "ok"


# ── the probe itself, against real sockets ──────────────────────────────────

def test_the_probe_sees_a_real_listener():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def _accept():
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                c, _ = srv.accept(); c.close()
            except OSError:
                pass

    t = threading.Thread(target=_accept, daemon=True); t.start()
    try:
        assert probe(port, timeout=5) is True
    finally:
        stop.set(); t.join(timeout=3); srv.close()


def test_the_probe_reports_a_dead_listener():
    """Bind and release, so the port is one nothing is listening on — the exact
    state a server that has lost its accept loop leaves behind."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert probe(port, timeout=2) is False


def test_the_probe_never_raises_on_a_nonsense_port():
    """It runs inside the scheduler loop; an exception here would take the
    schedules and the heartbeat down with it."""
    assert probe(0, timeout=1) is False
    assert probe(70000, timeout=1) is False


# ── it must be switchable off, and not armed in the wrong place ─────────────

def test_the_config_default_is_on_but_the_key_exists():
    from suni import config
    assert "listener_watchdog" in config.DEFAULTS
    assert config.DEFAULTS["listener_watchdog"] is True


def test_the_server_records_listening_state_on_the_heartbeat():
    """'Alive but deaf' has to be answerable from the black box afterwards, not
    only by probing the port while it is still happening."""
    src = (__import__("pathlib").Path(__file__).resolve().parent.parent
           / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    assert "_runstate.heartbeat(listening=" in src, \
        "the heartbeat still cannot distinguish serving from merely running"


def test_the_exit_path_records_its_reason_before_dying():
    """os._exit runs nothing afterwards, so the reason must be written first or
    the next start reports an unexplained kill."""
    src = (__import__("pathlib").Path(__file__).resolve().parent.parent
           / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    block = src.split('if _verdict == "exit":')[1].split("_os._exit(1)")[0]
    assert "mark_stopped(" in block, "it dies without saying why"
    assert "flush()" in block, "the CRITICAL line may never reach the log"
