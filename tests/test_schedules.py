"""
Scheduled invocations: cadence honesty, and the rules that hold when nobody is
watching.

The tool this replaces mapped any unrecognised schedule to DAILY, so "every
hour" silently became "every day" and reported success. Half of these tests
exist to make sure that cannot happen again; the rest cover the fact that a
scheduled run is unattended, which changes what is safe.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from suni import schedules


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(schedules, "SCHEDULES_DB", str(tmp_path / "schedules.db"))
    yield


# ── cadence: refuse rather than substitute ───────────────────────────────────
@pytest.mark.parametrize("bad", [
    "every hour and a half", "sometimes", "", "daily at 25:00",
    "weekly on xxx at 09:00", "every 0m", "cron(0 8 * * *)",
])
def test_unparseable_cadence_is_refused(bad):
    with pytest.raises(schedules.CadenceError):
        schedules.parse_cadence(bad)


@pytest.mark.parametrize("good,kind", [
    ("every 15m", "interval"), ("every 2h", "interval"), ("hourly", "hourly"),
    ("daily at 08:00", "daily"), ("weekly on mon at 09:30", "weekly"),
])
def test_supported_cadences(good, kind):
    assert schedules.parse_cadence(good)["kind"] == kind


def test_hourly_is_actually_hourly():
    """The specific bug: 'hourly' must not become 'daily'."""
    base = datetime(2026, 8, 20, 10, 30, tzinfo=timezone.utc)
    nxt = schedules.next_after("hourly", base)
    assert nxt - base < timedelta(hours=1.01)
    assert nxt.hour == 11 and nxt.minute == 0


def test_creating_with_a_bad_cadence_writes_nothing():
    with pytest.raises(schedules.CadenceError):
        schedules.create(name="x", prompt="p", cadence="whenever", owner_id="u1")
    assert schedules.list_for_user("u1") == []


def test_daily_rolls_to_tomorrow_when_the_time_has_passed():
    base = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
    nxt = schedules.next_after("daily at 08:00", base)
    assert nxt.day == 21 and nxt.hour == 8


def test_missed_slots_do_not_stack_up():
    """A machine asleep for a week must not wake and fire seven times: the next
    run is computed from now, not from the slot that was missed."""
    src = inspect.getsource(schedules.mark_ran)
    assert "next_after(cadence)" in src
    assert "_now()" in src


# ── ownership ────────────────────────────────────────────────────────────────
def test_only_the_owner_or_an_admin_can_delete():
    r = schedules.create(name="mine", prompt="p", cadence="hourly", owner_id="u1")
    assert schedules.delete(r["id"], "u2") is False
    assert schedules.delete(r["id"], "u2", "admin") is True


def test_users_see_only_their_own():
    schedules.create(name="a", prompt="p", cadence="hourly", owner_id="u1")
    schedules.create(name="b", prompt="p", cadence="hourly", owner_id="u2")
    assert len(schedules.list_for_user("u1")) == 1
    assert len(schedules.list_for_user("u1", "admin")) == 2


def test_due_returns_only_enabled_entries_past_their_time():
    r = schedules.create(name="soon", prompt="p", cadence="every 1m", owner_id="u1")
    assert schedules.due() == []                       # not yet
    later = datetime.now(timezone.utc) + timedelta(minutes=2)
    assert [d["id"] for d in schedules.due(later)] == [r["id"]]
    schedules.set_enabled(r["id"], False, "u1")
    assert schedules.due(later) == []


# ── the unattended rules, asserted against the runner ────────────────────────
def _runner_body(src: str) -> str:
    """The whole runner function, not a guessed number of characters.

    These assertions used to slice a fixed 3500 chars; adding four lines to the
    runner pushed the audit call past the window and failed a test about code
    that was still correct. A test that breaks when unrelated lines are inserted
    above what it checks is measuring the wrong thing.
    """
    i = src.index("async def _schedule_runner")
    end = src.index('@app.on_event("startup")', i)
    return src[i:end]


@pytest.fixture(scope="module")
def runner_src() -> str:
    # Anchored to this file, not the cwd: conftest chdirs into a tmpdir for
    # isolation, so a relative path here resolves somewhere with no source in it.
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    return (root / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")


def test_runs_as_the_owner_with_a_freshly_read_role(runner_src):
    body = _runner_body(runner_src)
    
    assert "_auth.get_user(s[\"owner_id\"])" in body, "the owner is not looked up at fire time"
    assert 'owner.get("role"' in body, "the role is not read from the owner record"
    assert "user_role=role" in body, "the run does not use the owner's role"


def test_a_deactivated_owner_stops_their_schedules(runner_src):
    body = _runner_body(runner_src)
    assert 'owner.get("active"' in body, \
        "a disabled account's schedules keep running"


def test_the_agent_is_rechecked_against_the_owner_at_fire_time(runner_src):
    body = _runner_body(runner_src)
    
    assert "list_for_user(owner[\"id\"], role)" in body, \
        "the agent is not re-checked against the owner when it fires"


def test_delivery_is_done_by_the_runner_not_the_model(runner_src):
    """No approver is present, so the model must not choose recipients."""
    body = _runner_body(runner_src)
    
    assert "_mail.send_email(d[\"to\"]" in body, "delivery is not performed by the runner"
    assert "delivery" in body


def test_every_run_is_audited(runner_src):
    body = _runner_body(runner_src)
    assert '"schedule.ran"' in body, "scheduled runs are not audited"


def test_a_failing_run_still_advances_the_clock(runner_src):
    """Otherwise one bad run wedges the schedule forever, retrying every tick."""
    body = _runner_body(runner_src)
    
    assert "except Exception as exc:" in body and "mark_ran" in body
