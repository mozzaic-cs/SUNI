"""Upkeep phase 1: SUNI keeps a book of what keeps going wrong.

The logs and diagnostics here are fixtures; nothing reads the machine's real
logs. Upkeep only reads, and one test holds the package to that.
"""
from __future__ import annotations

import pathlib

import pytest

from suni import diagnostics as D
from suni import erasure
from suni.upkeep import gaps, logscan, scan, store

ROOT = pathlib.Path(__file__).resolve().parent.parent

TB = [
    "Traceback (most recent call last):",
    '  File "C:\\Python312\\Lib\\site-packages\\starlette\\routing.py", line 74, in app',
    "    response = await f(request)",
    '  File "C:\\suni\\suni\\memory\\document_store.py", line {n}, in stats',
    "    for k, v in self._meta.items():",
    "RuntimeError: dictionary changed size during iteration",
]


def tb(n=212):
    return [l.replace("{n}", str(n)) for l in TB]


@pytest.fixture(autouse=True)
def _book(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "UPKEEP_DB", str(tmp_path / "upkeep.db"))
    yield


# ── reading the logs ─────────────────────────────────────────────────────────

def test_a_traceback_is_placed_in_suni_s_own_code_whatever_the_line():
    a = logscan.parse(tb(212), "startup.log", "t")
    b = logscan.parse(tb(240), "startup.log", "t")
    assert len(a) == 1
    assert a[0].location == "suni/memory/document_store.py:stats"
    assert a[0].in_suni and a[0].code == "RuntimeError"
    assert a[0].id == b[0].id, "an edit that moves the line must not make it a new problem"


def test_the_client_disconnect_wart_is_not_an_incident():
    lines = ["Traceback (most recent call last):",
             '  File "C:\\Python312\\Lib\\asyncio\\events.py", line 88, in _run',
             '  File "C:\\Python312\\Lib\\asyncio\\proactor_events.py", line 165, in _call_connection_lost',
             "    self._sock.shutdown(socket.SHUT_RDWR)",
             "ConnectionResetError: [WinError 10054] An existing connection was forcibly closed"]
    assert logscan.parse(lines * 5, "startup.log", "t") == []


def test_a_logged_error_and_its_traceback_are_one_event():
    lines = ["2026-11-09 10:00:00 | ERROR   | [CRASH] unhandled exception in run(): boom"] + tb()
    evs = logscan.parse(lines, "suni.log", "t")
    assert len(evs) == 1
    assert evs[0].kind == "traceback" and evs[0].location.endswith("document_store.py:stats")
    assert "[CRASH]" in evs[0].title
    assert evs[0].seen_at == "2026-11-09T10:00:00"


def test_a_chained_exception_is_one_event_named_by_the_last():
    lines = tb() + ["", "During handling of the above exception, another exception occurred:", "",
                    "Traceback (most recent call last):",
                    '  File "C:\\suni\\suni\\web\\server.py", line 9, in stats_api',
                    "    raise HTTPException(500)",
                    "fastapi.exceptions.HTTPException: 500"]
    evs = logscan.parse(lines, "startup.log", "t")
    assert len(evs) == 1
    assert evs[0].code == "fastapi.exceptions.HTTPException"


def test_the_same_message_with_other_numbers_is_the_same_problem():
    a = logscan.parse(["2026-11-09 10:00:00 | ERROR   | [MAIL] login failed for 'a@x.test' after 3 tries"], "l", "t")
    b = logscan.parse(["2026-11-09 11:00:00 | ERROR   | [MAIL] login failed for 'b@y.test' after 12 tries"], "l", "t")
    assert a[0].id == b[0].id


def test_a_print_style_error_is_caught():
    evs = logscan.parse(["Watcher error: [Errno 28] No space left on device"], "startup.log", "t")
    assert len(evs) == 1 and evs[0].code == "WATCHER"


def test_evidence_is_scrubbed():
    evs = logscan.parse(["2026-11-09 10:00:00 | ERROR   | [TG] bad token "
                         "telegram_bot_token=8869766917:AAEWezqZblWaljUDH0NyR1K"], "l", "t")
    assert "AAEWezqZblWaljUDH0NyR1K" not in evs[0].evidence + evs[0].title


def test_only_new_whole_lines_are_read(tmp_path):
    p = tmp_path / "startup.log"
    p.write_bytes(b"one\ntwo\nthr")
    assert logscan.read_new(p)[0] == ["one", "two"]
    with p.open("ab") as fh:
        fh.write(b"ee\nfour\n")
    assert logscan.read_new(p)[0] == ["three", "four"]
    assert logscan.read_new(p)[0] == []


def test_a_replaced_log_is_read_from_the_top(tmp_path):
    p = tmp_path / "startup.log"
    p.write_bytes(b"x" * 100 + b"\n")
    logscan.read_new(p)
    p.write_bytes(b"new start\n")
    lines, info = logscan.read_new(p)
    assert lines == ["new start"] and info["reset"]


def test_a_big_log_seen_for_the_first_time_starts_on_a_whole_line(tmp_path, monkeypatch):
    monkeypatch.setattr(logscan, "FIRST_WINDOW", 10)
    p = tmp_path / "startup.log"
    p.write_bytes(b"old line number one\nrecent\n")
    assert logscan.read_new(p)[0] == ["recent"]


# ── the book ─────────────────────────────────────────────────────────────────

class _Diag:
    def __init__(self):
        self.findings = []

    def run(self, log_path=""):
        return {"findings": list(self.findings), "checked": [], "took_s": 0}


@pytest.fixture
def world(tmp_path, monkeypatch):
    log = tmp_path / "startup.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setattr(scan, "log_paths", lambda: [log])
    logscan.read_new(log)               # upkeep has been watching this log
    d = _Diag()
    monkeypatch.setattr(D, "run", d.run)
    return log, d


def _append(path, lines):
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _one(kind):
    rows = store.list_incidents(kind)
    assert len(rows) == 1, rows
    return rows[0]


def test_a_finding_needs_two_scans_and_clears_by_itself(world):
    _, d = world
    d.findings = [D.Finding(D.BROKEN, "2 turn(s) began and never finished", code="turn.unfinished")]
    scan.scan()
    assert _one("finding")["state"] == "watching", "seen once may be a turn still running"
    d.findings = [D.Finding(D.BROKEN, "3 turn(s) began and never finished", code="turn.unfinished")]
    scan.scan()
    row = _one("finding")
    assert row["state"] == "open" and row["title"].startswith("3 turn")
    d.findings = []
    scan.scan()
    assert _one("finding")["state"] == "resolved"


def test_a_finding_seen_once_and_gone_leaves_nothing(world):
    _, d = world
    d.findings = [D.Finding(D.SLOW, "slowest recent turn took 97s", code="turn.slow")]
    scan.scan()
    d.findings = []
    scan.scan()
    assert store.list_incidents("finding") == []


def test_notes_and_oks_are_not_incidents(world):
    _, d = world
    d.findings = [D.Finding(D.NOTE, "no messaging channels are enabled", code="channel.none"),
                  D.Finding(D.OK, "every tool fits", code="config.tools_fit")]
    scan.scan(); scan.scan()
    assert store.list_incidents() == []


def test_an_error_is_counted_not_repeated(world):
    log, _ = world
    _append(log, tb() * 3)
    scan.scan()
    _append(log, tb(300))
    scan.scan()
    row = _one("error")
    assert row["count"] == 4 and row["state"] == "open" and row["in_suni"]


def test_what_a_log_held_before_upkeep_watched_it_is_history(tmp_path, monkeypatch):
    """startup.log has no timestamps: a month-old traceback read on the first
    scan would otherwise be "open, last seen today"."""
    log = tmp_path / "startup.log"
    log.write_text("\n".join(tb()) + "\n", encoding="utf-8")
    monkeypatch.setattr(scan, "log_paths", lambda: [log])
    monkeypatch.setattr(D, "run", _Diag().run)
    scan.scan()
    row = _one("error")
    assert row["state"] == "resolved" and "before upkeep" in row["state_note"]
    _append(log, tb())                  # it happens again, now
    scan.scan()
    row = _one("error")
    assert row["state"] == "open" and row["count"] == 2


def test_a_new_day_s_log_is_news_not_history(tmp_path, monkeypatch):
    """Every day brings a new suni_*.log, so "a log never read before" is not
    the same as "from before upkeep watched": only the very first scan is."""
    old = tmp_path / "startup.log"
    old.write_text("", encoding="utf-8")
    paths = [old]
    monkeypatch.setattr(scan, "log_paths", lambda: list(paths))
    monkeypatch.setattr(D, "run", _Diag().run)
    scan.scan()
    today = tmp_path / "suni_2026-11-10.log"
    today.write_text("2026-11-10 00:05:00 | ERROR   | [MAIL] login failed\n", encoding="utf-8")
    paths.append(today)
    scan.scan()
    assert _one("error")["state"] == "open"


def test_a_dismissal_is_audited_under_the_admin_s_id(client, admin_headers, test_users):
    """erasure.py finds a person's audit rows by user id, not by name."""
    from suni import audit
    store.record(id="aud1", kind="error", title="boom")
    client.post("/api/upkeep/incidents/aud1/dismiss", headers=admin_headers, json={})
    rows, _ = audit.query(limit=50, route="upkeep.incident.dismissed")
    assert rows and rows[0]["user_id"] == test_users["admin"]["id"]


def test_a_dismissed_error_stays_dismissed_but_is_still_counted(world):
    log, _ = world
    _append(log, tb())
    scan.scan()
    store.set_state(_one("error")["id"], "dismissed", by="admin_test")
    _append(log, tb())
    scan.scan()
    row = _one("error")
    assert row["state"] == "dismissed" and row["count"] == 2


def test_a_resolved_error_that_comes_back_is_open_again(world):
    log, _ = world
    _append(log, tb())
    scan.scan()
    store.set_state(_one("error")["id"], "resolved")
    _append(log, tb())
    scan.scan()
    row = _one("error")
    assert row["state"] == "open" and "came back" in row["state_note"]


def test_a_quiet_error_resolves_after_a_week(world, monkeypatch):
    log, _ = world
    _append(log, ["2026-01-01 10:00:00 | ERROR   | [OLD] something broke"])
    scan.scan()
    assert _one("error")["state"] == "resolved"


def test_one_scan_at_a_time(world):
    assert scan._lock.acquire(blocking=False)
    try:
        assert scan.scan().get("busy")
    finally:
        scan._lock.release()


# ── what she could not do ────────────────────────────────────────────────────

@pytest.mark.parametrize("reply,yes", [
    ("I can't join the call, but I can record this machine's audio.", True),
    ("I'm unable to open that file.", True),
    ("Não consigo entrar na chamada.", True),
    ("Não tenho acesso ao teu calendário.", True),
    ("Here is the transcript you asked for.", False),
    ("x" * 400 + " I can't", False),
])
def test_a_refusal_is_recognised_in_english_and_portuguese(reply, yes):
    assert gaps.is_refusal(reply) is yes


def test_gaps_are_noted_only_when_upkeep_is_on(monkeypatch):
    from suni import config as _cfg
    on = {"v": False}
    real = _cfg.get
    monkeypatch.setattr(_cfg, "get", lambda k, d=None: on["v"] if k == "upkeep_enabled" else real(k, d))
    gaps.note_reply("u1", "send me the Meet transcript", "I can't join the call.")
    assert store.list_gaps() == []
    on["v"] = True
    gaps.note_reply("u1", "send me the Meet transcript", "I can't join the call.")
    gaps.note_reply("u1", "Send me the meet transcript!", "I can't join the call.")
    gaps.note_reply("u2", "send me the Meet transcript", "I can't join the call.")
    rows = store.list_gaps()
    assert sorted((r["user_id"], r["count"]) for r in rows) == [("u1", 2), ("u2", 1)]


def test_gaps_are_erased_with_the_person():
    assert ("upkeep.db", "gaps", "user_id", "capability_gaps") in erasure.SUBJECT_TABLES


def test_a_turn_notes_its_reply():
    src = (ROOT / "suni/core/orchestrator.py").read_text(encoding="utf-8")
    assert "_gaps.note_reply, user_id, user_input, result" in src


# ── it only reads ────────────────────────────────────────────────────────────

def test_upkeep_changes_nothing_but_its_own_book():
    """Phase 1 watches. Restarting, writing config or running anything is a
    later phase, behind an approval — not something to slip in here."""
    for f in (ROOT / "suni" / "upkeep").glob("*.py"):
        src = f.read_text(encoding="utf-8")
        for banned in ("subprocess", "os.system", "config.save", "_cfg.set", ".write_text(",
                       "write_bytes(", "shutil.", "os.remove", "unlink("):
            assert banned not in src, f"{f.name} uses {banned}"


def test_every_diagnostics_finding_has_a_code():
    import ast
    tree = ast.parse((ROOT / "suni/diagnostics.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "Finding"]
    assert len(calls) > 15
    missing = [n.lineno for n in calls if not any(k.arg == "code" for k in n.keywords)]
    assert not missing, f"a finding without a code cannot be tracked: lines {missing}"


# ── the admin side ───────────────────────────────────────────────────────────

def test_the_book_is_for_admins_only(client, admin_headers, std_headers):
    assert client.get("/api/upkeep", headers=std_headers).status_code == 403
    assert client.post("/api/upkeep/scan", headers=std_headers).status_code == 403
    r = client.get("/api/upkeep", headers=admin_headers)
    assert r.status_code == 200
    assert {"enabled", "incidents", "gaps", "last_scan"} <= set(r.json())


def test_dismiss_and_reopen_from_the_panel(client, admin_headers):
    store.record(id="abc", kind="error", title="boom")
    r = client.post("/api/upkeep/incidents/abc/dismiss", headers=admin_headers, json={})
    assert r.status_code == 200 and r.json()["state"] == "dismissed"
    assert r.json()["state_by"] == "admin_test"
    assert client.post("/api/upkeep/incidents/abc/reopen", headers=admin_headers,
                       json={}).json()["state"] == "open"
    assert client.post("/api/upkeep/incidents/abc/delete", headers=admin_headers,
                       json={}).status_code == 404
    assert client.post("/api/upkeep/incidents/nope/dismiss", headers=admin_headers,
                       json={}).status_code == 404


def test_the_tab_is_in_the_panel():
    html = (ROOT / "suni/web/admin.html").read_text(encoding="utf-8")
    assert "switchTab('upkeep',this)" in html and 'id="panel-upkeep"' in html
    assert "if (name === 'upkeep') loadUpkeep();" in html
    i18n = (ROOT / "suni/web/i18n.js").read_text(encoding="utf-8")
    assert i18n.count("'admin.upk_intro'") == 2, "English and Portuguese"
