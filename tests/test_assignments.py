"""Work handed to an agent that outlives the request that asked for it.

The queue was half-built: statuses, progress streaming and completion notices all
worked, and nothing ever created a row — `create_task` had no callers and the only
task in the live database was a test from June. These cover the half that was
missing, and the failure mode that half brings with it: a task lives in SQLite but
RUNS in memory, so a restart leaves rows claiming to be running with nothing
running them.
"""
import asyncio
import inspect

import pytest

from suni import task_queue as tq


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(tq, "_DB", tmp_path / "bg_tasks.db")
    yield


def test_an_assignment_records_who_does_it_and_what_to_ask():
    t = tq.create_task(title="Renewals", description="check renewals",
                       user_id="u1", agent_slug="bookkeeper",
                       prompt="Which clients renew before December?")
    got = tq.get_task(t["id"])
    assert got["agent_slug"] == "bookkeeper"
    assert got["prompt"] == "Which clients renew before December?"
    assert got["status"] == "pending"


def test_the_work_runs_and_the_result_is_kept():
    t = tq.create_task(title="T", user_id="u1", prompt="do it")

    async def work():
        return "forty-two"

    asyncio.run(tq.run_task(t["id"], work))
    done = tq.get_task(t["id"])
    assert done["status"] == "done" and done["result"] == "forty-two"
    assert done["completed_at"]


def test_a_failure_is_recorded_rather_than_lost():
    t = tq.create_task(title="T", user_id="u1", prompt="do it")

    async def work():
        raise RuntimeError("the agent refused")

    asyncio.run(tq.run_task(t["id"], work))
    failed = tq.get_task(t["id"])
    assert failed["status"] == "failed"
    assert "refused" in failed["error"]


def test_tasks_are_listed_per_user():
    tq.create_task(title="mine", user_id="u1", prompt="x")
    tq.create_task(title="theirs", user_id="u2", prompt="x")
    assert [t["title"] for t in tq.list_tasks(user_id="u1")] == ["mine"]


# ── the restart case ─────────────────────────────────────────────────────────
def test_interrupted_work_is_failed_at_startup_not_left_running():
    running = tq.create_task(title="was running", user_id="u1", prompt="x")
    tq._update(running["id"], status="running")
    pending = tq.create_task(title="never started", user_id="u1", prompt="x")

    assert tq.reap_orphans() == 2
    for tid in (running["id"], pending["id"]):
        t = tq.get_task(tid)
        assert t["status"] == "failed", "a row nothing is running must not say running"
        assert "interrupted" in t["error"]


def test_reaping_leaves_finished_work_alone():
    t = tq.create_task(title="done already", user_id="u1", prompt="x")
    tq._update(t["id"], status="done", result="kept")
    assert tq.reap_orphans() == 0
    assert tq.get_task(t["id"])["result"] == "kept"


def test_an_upgraded_database_gains_the_new_columns(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "old.db"
    sqlite3.connect(db).executescript("""
        CREATE TABLE bg_tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending', user_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, started_at TEXT DEFAULT NULL,
            completed_at TEXT DEFAULT NULL, result TEXT DEFAULT NULL,
            error TEXT DEFAULT NULL, progress TEXT NOT NULL DEFAULT '0',
            notify_channel TEXT DEFAULT '');
    """)
    monkeypatch.setattr(tq, "_DB", db)
    t = tq.create_task(title="T", user_id="u1", agent_slug="a", prompt="p")
    assert tq.get_task(t["id"])["agent_slug"] == "a"


# ── wiring ───────────────────────────────────────────────────────────────────
def test_the_endpoint_exists_and_checks_the_agent_is_the_callers_to_use():
    from suni.web import server
    src = inspect.getsource(server)
    i = src.index('@app.post("/api/assignments")')
    block = src[i:i + 3000]
    assert "list_for_user" in block, "any user could assign work to any agent"
    assert "run_task" in block and "_safe_run" in block
    assert "create_task" in block


def test_startup_reaps_interrupted_tasks():
    from suni.web import server
    src = inspect.getsource(server)
    assert "reap_orphans()" in src, "interrupted tasks would claim to be running for ever"
