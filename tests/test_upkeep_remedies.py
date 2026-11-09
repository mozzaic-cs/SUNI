"""Upkeep phase 2: the short list of things SUNI may fix on her own.

Nothing here touches the machine: channels, Ollama, processes and the clock
are all stand-ins. The probes are faked; the decisions are the real code.
"""
from __future__ import annotations

import asyncio
import pathlib
import socket

import pytest

from suni.upkeep import health, idle, remedies, store

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _book(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "UPKEEP_DB", str(tmp_path / "upkeep.db"))
    monkeypatch.setattr(idle, "_turns", {"active": 0, "last_end": 0.0})
    monkeypatch.setattr(remedies, "CHANNEL_RECHECK_S", 0)
    monkeypatch.setattr(remedies, "OLLAMA_POLL_S", 0)
    yield
    health.register_channel_status(None)
    remedies.register_channel_restart(None)


def _open(code, id="inc1"):
    store.record(id=id, kind="finding", code=code, level="broken", title=code,
                 source="diagnostics")
    return id


def _actions():
    return list(reversed(store.list_actions()))


class Channels:
    """A stand-in for the server's channel supervisor."""
    def __init__(self, running=False, stays_up=True):
        self.running, self.stays_up, self.restarts = running, stays_up, 0
        health.register_channel_status(self.status)
        remedies.register_channel_restart(self.restart)

    def status(self):
        return {"telegram": {"should": True, "running": self.running, "error": ""}}

    async def restart(self, name):
        assert name == "telegram"
        self.restarts += 1
        self.running = self.stays_up


# ── is she busy ──────────────────────────────────────────────────────────────

def test_a_turn_running_or_just_finished_is_busy():
    assert idle.busy_reasons() == []
    idle.turn_started()
    assert any("running" in r for r in idle.busy_reasons())
    idle.turn_finished()
    assert any("finished" in r for r in idle.busy_reasons()), "between two turns is not idle"
    assert idle.busy_reasons(quiet_seconds=0) == []


def test_a_meeting_being_recorded_is_busy(monkeypatch):
    from suni import meetings
    monkeypatch.setattr(meetings, "_active", {"someone": {"meeting_id": "m"}})
    assert idle.busy_reasons() == ["a meeting is being recorded"]


def test_a_code_task_running_is_busy(monkeypatch):
    from suni.tools import code_project

    async def go():
        lock = asyncio.Lock()
        await lock.acquire()
        monkeypatch.setattr(code_project, "_locks", {"proj": lock})
        return idle.busy_reasons()
    assert asyncio.run(go()) == ["a code task is running"]


def test_a_scheduled_run_in_flight_is_busy(monkeypatch):
    from suni import updater
    monkeypatch.setattr(updater, "in_flight", lambda: 1)
    assert idle.busy_reasons() == ["a scheduled run is in flight"]


def test_every_turn_is_counted():
    src = (ROOT / "suni/core/orchestrator.py").read_text(encoding="utf-8")
    body = src[src.index("async def _safe_run"):src.index("def _tool_permissions")]
    assert "_idle.turn_started()" in body
    finally_part = body[body.rindex("finally:"):]
    assert "_idle.turn_finished()" in finally_part, "a turn that raises must still end"


# ── what it looks for ────────────────────────────────────────────────────────

def test_a_channel_switched_on_but_not_running_is_found():
    Channels(running=False)
    codes = [f.code for f in health.findings() if f.code.startswith("channel")]
    assert codes == ["channel.telegram.down"]


def test_a_channel_switched_off_is_not_a_problem():
    health.register_channel_status(lambda: {"telegram": {"should": False, "running": False}})
    assert not [f for f in health.findings() if f.code.startswith("channel")]


@pytest.mark.parametrize("cfg, expect", [
    ({"embed_backend": "ollama", "embed_base_url": "http://localhost:11434"}, ("localhost", 11434)),
    ({"embed_backend": "ollama", "embed_base_url": "http://127.0.0.1:9999"}, ("127.0.0.1", 9999)),
    ({"embed_backend": "ollama", "embed_base_url": "http://gpu-box:11434"}, None),
    ({"embed_backend": "openai", "embed_base_url": "http://localhost:11434"}, None),
])
def test_only_a_local_ollama_that_memory_uses_is_watched(monkeypatch, cfg, expect):
    from suni import config as C
    monkeypatch.setattr(C, "get", lambda k, d=None: cfg.get(k, d))
    assert health.ollama_address() == expect


def test_ollama_down_is_found_and_up_is_not(monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        monkeypatch.setattr(health, "ollama_address", lambda: ("127.0.0.1", port))
        assert not [f for f in health.findings() if f.code == "ollama.down"]
    finally:
        srv.close()
    assert [f.code for f in health.findings() if f.code == "ollama.down"] == ["ollama.down"]


def test_codes_map_to_their_remedy():
    assert remedies.match("channel.telegram.down") == (remedies.RESTART_CHANNEL, "telegram")
    assert remedies.match("ollama.down") == (remedies.START_OLLAMA, "")
    assert remedies.match("channel.telegram.no_allowlist") is None
    assert remedies.match("turn.unfinished") is None


# ── how far it goes ──────────────────────────────────────────────────────────

def test_level_zero_does_nothing():
    ch = Channels()
    _open("channel.telegram.down")
    assert asyncio.run(remedies.run(0)) == []
    assert ch.restarts == 0 and _actions() == []


def test_a_dry_run_only_says_what_it_would_do():
    ch = Channels()
    _open("channel.telegram.down")
    asyncio.run(remedies.run(1))
    assert ch.restarts == 0
    a = _actions()
    assert [(x["mode"], x["outcome"]) for x in a] == [("dry", "would")]
    assert "restart the telegram channel" in a[0]["detail"]
    assert store.get("inc1")["state"] == "open"


def test_a_dry_run_stops_at_the_same_limit_and_changes_nothing():
    Channels()
    _open("channel.telegram.down")
    for _ in range(remedies.RESTART_CHANNEL.max_tries + 3):
        asyncio.run(remedies.run(1))
    outcomes = [x["outcome"] for x in _actions()]
    assert outcomes == ["would"] * remedies.RESTART_CHANNEL.max_tries + ["gave_up"]
    assert store.get("inc1")["state_note"] == "", "a dry run leaves the book's states alone"


def test_a_dead_channel_is_restarted_and_the_incident_resolved():
    ch = Channels()
    _open("channel.telegram.down")
    asyncio.run(remedies.run(2))
    assert ch.restarts == 1
    assert [x["outcome"] for x in _actions()] == ["fixed"]
    row = store.get("inc1")
    assert row["state"] == "resolved" and row["state_note"].startswith("fixed by upkeep")


def test_a_fix_that_does_not_hold_is_handed_to_a_person():
    ch = Channels(stays_up=False)
    _open("channel.telegram.down")
    for _ in range(remedies.RESTART_CHANNEL.max_tries + 3):
        asyncio.run(remedies.run(2))
    assert ch.restarts == remedies.RESTART_CHANNEL.max_tries
    outcomes = [x["outcome"] for x in _actions()]
    assert outcomes == ["failed"] * remedies.RESTART_CHANNEL.max_tries + ["gave_up"]
    row = store.get("inc1")
    assert row["state"] == "open" and row["state_note"].startswith("needs a person")


def test_a_fix_that_came_back_counts_against_the_limit():
    ch = Channels()
    for _ in range(remedies.RESTART_CHANNEL.max_tries + 1):
        ch.running = False
        _open("channel.telegram.down")         # the finding came back: re-opened
        asyncio.run(remedies.run(2))
    assert ch.restarts == remedies.RESTART_CHANNEL.max_tries
    assert _actions()[-1]["outcome"] == "gave_up"


def test_dismissed_and_watching_incidents_are_left_alone():
    ch = Channels()
    _open("channel.telegram.down")
    store.set_state("inc1", "dismissed")
    store.record(id="inc2", kind="finding", code="channel.telegram.down", title="x",
                 initial_state="watching")
    asyncio.run(remedies.run(2))
    assert ch.restarts == 0 and _actions() == []


def test_a_remedy_that_needs_idle_waits_while_she_is_busy(monkeypatch):
    ch = Channels()
    _open("channel.telegram.down")
    patient = remedies.Remedy("restart_channel", 3, 6, needs_idle=True)
    monkeypatch.setattr(remedies, "match", lambda code: (patient, "telegram"))
    idle.turn_started()
    asyncio.run(remedies.run(2))
    assert ch.restarts == 0 and _actions() == []
    idle.turn_finished()
    monkeypatch.setattr(idle, "QUIET_SECONDS", 0)
    monkeypatch.setattr(idle, "busy_reasons", lambda quiet_seconds=0: [])
    asyncio.run(remedies.run(2))
    assert ch.restarts == 1


# ── Ollama: start it, never stop it ──────────────────────────────────────────

@pytest.fixture
def ollama(monkeypatch, tmp_path):
    app = tmp_path / "ollama app.exe"
    app.write_text("")
    state = {"up": False, "procs": [], "spawned": []}
    monkeypatch.setattr(health, "ollama_address", lambda: ("127.0.0.1", 11434))
    monkeypatch.setattr(health, "listening", lambda h, p, timeout=3.0: state["up"])
    monkeypatch.setattr(remedies, "_ollama_processes", lambda: list(state["procs"]))
    monkeypatch.setattr(remedies, "_ollama_app", lambda: app)
    monkeypatch.setattr(remedies.sys, "platform", "win32")

    def spawn(path):
        state["spawned"].append(path)
        state["up"] = True
    monkeypatch.setattr(remedies, "_spawn_detached", spawn)
    return state


def test_a_stopped_ollama_is_started(ollama):
    _open("ollama.down")
    asyncio.run(remedies.run(2))
    assert len(ollama["spawned"]) == 1
    assert [x["outcome"] for x in _actions()] == ["fixed"]
    assert store.get("inc1")["state"] == "resolved"


def test_a_running_but_silent_ollama_is_left_to_a_person(ollama):
    ollama["procs"] = ["ollama.exe"]
    _open("ollama.down")
    asyncio.run(remedies.run(2))
    asyncio.run(remedies.run(2))
    assert ollama["spawned"] == []
    assert [x["outcome"] for x in _actions()] == ["refused"], "asked once, not every scan"
    assert "does not stop programs" in store.get("inc1")["state_note"]


def test_ollama_is_only_started_as_the_windows_app(ollama, monkeypatch):
    monkeypatch.setattr(remedies.sys, "platform", "linux")
    _open("ollama.down")
    asyncio.run(remedies.run(2))
    assert ollama["spawned"] == [] and _actions()[0]["outcome"] == "refused"


def test_an_ollama_that_never_answers_is_a_failed_fix(ollama, monkeypatch):
    monkeypatch.setattr(remedies, "_spawn_detached", lambda p: ollama["spawned"].append(p))
    _open("ollama.down")
    asyncio.run(remedies.run(2))
    assert [x["outcome"] for x in _actions()] == ["failed"]
    assert store.get("inc1")["state"] == "open"


# ── what it is allowed to do at all ──────────────────────────────────────────

def test_the_list_of_fixes_is_short_and_fixed():
    assert set(remedies.ACTIONS) == {"restart_channel", "start_ollama"}


def test_remedies_write_no_config_and_stop_nothing():
    src = (ROOT / "suni/upkeep/remedies.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    code = code[code.index('"""', code.index('"""') + 3) + 3:]      # past the docstring
    for banned in ("config.save", "_cfg.set", "suni_config", "os.remove", "unlink(", "shutil.",
                   ".kill(", "terminate(", "os.system", "restart-suni", ".write_text(",
                   "write_bytes(", "taskkill", "Stop-"):
        assert banned not in code, f"remedies.py uses {banned}"
    assert code.count("proc.popen(") == 2, "one launch, with and without job breakaway"
    assert "subprocess.Popen(" not in code


def test_fixes_are_off_unless_switched_on():
    from suni import config
    assert config.DEFAULTS["upkeep_level"] == 0


def test_the_tab_shows_the_level_what_was_done_and_whether_she_is_busy(client, admin_headers):
    d = client.get("/api/upkeep", headers=admin_headers).json()
    assert {"level", "busy", "actions"} <= set(d)
    html = (ROOT / "suni/web/admin.html").read_text(encoding="utf-8")
    assert 'id="upk-level"' in html and 'id="upk-act-table"' in html
