"""A schedule that cannot run must stop asking, not fail forever.

Before this, a failure did exactly what a success did: write the status and wait
for the next slot. A daily job whose password had expired failed at 08:00 every
morning for as long as nobody read the list, and a transient failure — a minute
of no network — waited a full day for a retry that would have worked at once.

One retry soon, then stop. Disabled rather than deleted, with the reason on the
record, because the owner has to see what happened and switch it back on.
"""
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from suni import schedules as sch


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(sch, "SCHEDULES_DB", str(tmp_path / "schedules.db"))
    yield


def _make(name="Nightly", cadence="daily at 08:00"):
    return sch.create(name=name, prompt="do the thing", cadence=cadence,
                      owner_id="owner", owner_name="owner")


def test_the_first_failure_asks_again_soon_not_tomorrow():
    s = _make()
    out = sch.mark_failed(s["id"], "error: network down", s["cadence"])
    assert out["action"] == "retry" and out["streak"] == 1
    after = sch.get(s["id"])
    assert after["enabled"] is True, "one failure must not disable a schedule"
    gap = (datetime.fromisoformat(after["next_run"])
           - datetime.now(timezone.utc)).total_seconds()
    assert 0 < gap <= sch.RETRY_AFTER_S + 5, "the retry should be soon, not at the next slot"
    assert "retrying" in after["last_status"] and "network down" in after["last_status"]


def test_the_second_failure_stops_the_schedule_with_a_reason():
    s = _make()
    sch.mark_failed(s["id"], "error: SMTP refused", s["cadence"])
    out = sch.mark_failed(s["id"], "error: SMTP refused", s["cadence"])
    assert out["action"] == "stopped"
    after = sch.get(s["id"])
    assert after["enabled"] is False, "a job failing twice must stop grinding"
    assert "stopped after 2 failures" in after["last_status"]
    assert "SMTP refused" in after["last_status"], "the owner needs to know why"


def test_a_stopped_schedule_is_not_deleted():
    s = _make()
    sch.mark_failed(s["id"], "e", s["cadence"])
    sch.mark_failed(s["id"], "e", s["cadence"])
    assert sch.get(s["id"]) is not None
    # And it can be switched back on, which is the point of disabling not deleting.
    assert sch.set_enabled(s["id"], True, "owner") is True
    assert sch.get(s["id"])["enabled"] is True


def test_a_success_clears_the_streak_so_one_bad_night_is_not_fatal():
    s = _make()
    sch.mark_failed(s["id"], "error: transient", s["cadence"])
    sch.mark_ran(s["id"], "ok", s["cadence"])
    assert sch.get(s["id"])["fail_streak"] == 0
    # The next failure is therefore a first failure again: a retry, not a stop.
    assert sch.mark_failed(s["id"], "error: again", s["cadence"])["action"] == "retry"


def test_a_stopped_schedule_stops_coming_up_as_due():
    s = _make(cadence="every 5 minutes")
    sch.mark_failed(s["id"], "e", s["cadence"])
    sch.mark_failed(s["id"], "e", s["cadence"])
    later = datetime.now(timezone.utc) + timedelta(days=2)
    assert all(d["id"] != s["id"] for d in sch.due(later))


def test_an_unparseable_cadence_still_gets_a_next_run():
    s = _make()
    out = sch.mark_failed(s["id"], "e", "not a cadence at all")
    assert out["action"] == "retry"
    assert sch.get(s["id"])["next_run"], "a schedule with no next_run is unrecoverable"


def test_an_upgraded_database_gains_the_column(tmp_path, monkeypatch):
    """The table predates fail_streak, and CREATE TABLE IF NOT EXISTS does
    nothing to a table that already exists."""
    import sqlite3
    db = tmp_path / "old.db"
    sqlite3.connect(db).executescript("""
        CREATE TABLE schedules (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_id TEXT NOT NULL,
            owner_name TEXT NOT NULL DEFAULT '', prompt TEXT NOT NULL,
            agent_slug TEXT NOT NULL DEFAULT '', cadence TEXT NOT NULL,
            delivery TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1,
            next_run TEXT NOT NULL, last_run TEXT NOT NULL DEFAULT '',
            last_status TEXT NOT NULL DEFAULT '', run_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL);
    """)
    monkeypatch.setattr(sch, "SCHEDULES_DB", str(db))
    s = _make()
    assert sch.mark_failed(s["id"], "e", s["cadence"])["action"] == "retry"


def test_the_runner_uses_the_recovery_path():
    from suni.web import server
    src = inspect.getsource(server)
    i = src.index('"schedule.ran"')
    block = src[i - 1500:i]
    assert "mark_failed" in block, "the runner still records failures as ordinary runs"
    assert "mark_ran" in block, "a successful run must still advance normally"
    # The unavailable-agent branch is the same class of silent forever-failure.
    assert 'mark_failed(\n                                s["id"], "agent not available' in src \
        or 'mark_failed(' in src[src.index("agent not available") - 400:
                                 src.index("agent not available")]
