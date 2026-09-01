"""
The black box — does it actually record a death?

Written because the thing it replaces failed silently: SUNI stopped on 28 Aug
2026 and stayed down three days with nothing in any log saying when or why. A
diagnostic that only works when someone remembers to check it is the same
failure again, so these tests drive the real sequence — start, heartbeat, die —
and assert on the file and the WARNING, not on the functions being callable.

The load-bearing case is the last one: a first run must NOT report a crash.
A warning that cries wolf on every fresh install is worse than no warning,
because it trains the operator to skip the one that matters.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from suni import runstate


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point the module at a throw-away file, not the live instance's."""
    monkeypatch.setattr(runstate, "_PATH", tmp_path / "memory" / "runstate.json")
    yield


def _state() -> dict:
    return json.loads(runstate._PATH.read_text(encoding="utf-8"))


# ── a run that ends badly ────────────────────────────────────────────────────

def test_a_killed_run_is_reported_as_unclean_on_the_next_start(caplog):
    """The whole point: no shutdown code ran, so the file still says 'running'."""
    runstate.mark_started()
    runstate.heartbeat()
    # …process is killed here. Nothing else is written.

    with caplog.at_level(logging.WARNING):
        prev = runstate.mark_started()

    assert prev["unclean"] is True
    assert prev["last_seen"], "an unclean stop with no last-seen time is useless"
    assert any("WITHOUT a clean shutdown" in r.message for r in caplog.records), \
        "the operator is never told; this is the silent outage all over again"


def test_the_unclean_report_says_when_it_was_last_alive():
    """'It is down' was already known. 'It was last alive at T' is the new fact."""
    runstate.mark_started()
    runstate.heartbeat()
    beat = _state()["last_heartbeat"]

    prev = runstate.mark_started()
    assert prev["last_seen"] == beat
    assert prev["ran_for"] != "unknown"


def test_a_clean_shutdown_is_not_reported_as_a_crash(caplog):
    runstate.mark_started()
    runstate.mark_stopped("clean shutdown")

    with caplog.at_level(logging.WARNING):
        prev = runstate.mark_started()

    assert prev["unclean"] is False
    assert prev["reason"] == "clean shutdown"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], \
        "a clean stop must not raise a warning"


def test_a_first_run_is_not_a_crash(caplog):
    """No file at all means a fresh install, NOT a death. Crying wolf here is
    how the real warning gets ignored later."""
    assert not runstate._PATH.exists()
    with caplog.at_level(logging.WARNING):
        prev = runstate.mark_started()
    assert prev["unclean"] is False
    assert prev["first_run"] is True
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ── the mechanics ────────────────────────────────────────────────────────────

def test_mark_started_claims_the_file_for_this_process():
    import os
    runstate.mark_started()
    s = _state()
    assert s["status"] == "running"
    assert s["pid"] == os.getpid()
    assert s["started_at"] and s["last_heartbeat"]


def test_heartbeat_moves_only_the_heartbeat():
    runstate.mark_started()
    before = _state()
    runstate.heartbeat()
    after = _state()
    assert after["started_at"] == before["started_at"], "start time was rewritten"
    assert after["status"] == "running"
    assert after["last_heartbeat"] >= before["last_heartbeat"]


def test_a_heartbeat_before_any_start_writes_nothing():
    """Non-server entrypoints (CLI, MCP) import this module too; they must not
    create a phantom run that the next server start reports as a crash."""
    runstate.heartbeat()
    assert not runstate._PATH.exists()


# ── it must never take the server down ───────────────────────────────────────

def test_an_unwritable_path_is_survived_silently(monkeypatch, tmp_path):
    """Diagnostics failing must not fail the flight."""
    monkeypatch.setattr(runstate, "_PATH", tmp_path / "nope" / "x.json")

    def _boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", _boom)
    runstate.mark_started()      # must not raise
    runstate.heartbeat()
    runstate.mark_stopped()


def test_a_corrupt_file_reads_as_a_first_run_not_a_crash():
    runstate._PATH.parent.mkdir(parents=True, exist_ok=True)
    runstate._PATH.write_text("{not json", encoding="utf-8")
    prev = runstate.previous_run()
    assert prev["unclean"] is False


def test_it_holds_nothing_about_any_user():
    """It sits in memory/ beside the stores erasure sweeps, but it is not a user
    store and must never become one — no field here may key on a person."""
    runstate.mark_started()
    runstate.heartbeat()
    keys = set(_state())
    assert keys <= {"status", "pid", "started_at", "last_heartbeat",
                    "reason", "stopped_at",
                    # whether the accept loop is alive — about the PROCESS,
                    # not about any person
                    "listening"}, f"unexpected field: {keys}"
