"""What is on the screen, and who is allowed to be told.

Window titles are the cheapest useful signal on a desktop and the richest for
their size — and they are somebody's day in plain text: client names, document
names, an email address in a mail window's title. Every test here is about a way
that could leak.
"""
from __future__ import annotations

import inspect

import pytest

from suni import desktop


class Cfg:
    def __init__(self, **kw):
        self.d = kw

    def get(self, key, default=None):
        return self.d.get(key, default)


@pytest.fixture(autouse=True)
def _no_history():
    desktop.clear()
    yield
    desktop.clear()


@pytest.fixture
def fake_screen(monkeypatch):
    def _set(windows, focused=None):
        monkeypatch.setattr(desktop, "snapshot",
                            lambda config=None: {"at": 0, "focused": focused,
                                                 "windows": windows})
    return _set


# ── the gates ────────────────────────────────────────────────────────────────
def test_off_by_default():
    from suni.config import DEFAULTS
    assert DEFAULTS["desktop_awareness"] is False
    assert DEFAULTS["desktop_owner"] == ""


def test_nothing_is_reported_when_it_is_switched_off(fake_screen):
    fake_screen([{"app": "excel.exe", "title": "Budget.xlsx"}])
    assert desktop.context_line("u1", Cfg(desktop_awareness=False, desktop_owner="u1")) == ""


def test_with_no_owner_named_nobody_is_told(fake_screen):
    """A shared instance must not quietly report one person's desktop."""
    fake_screen([{"app": "excel.exe", "title": "Budget.xlsx"}])
    cfg = Cfg(desktop_awareness=True, desktop_owner="")
    assert desktop.context_line("u1", cfg) == ""
    assert desktop.context_line("", cfg) == ""


def test_only_the_owner_is_told(fake_screen):
    fake_screen([{"app": "excel.exe", "title": "Budget.xlsx"}])
    cfg = Cfg(desktop_awareness=True, desktop_owner="owner")
    assert "Budget.xlsx" in desktop.context_line("owner", cfg)
    assert desktop.context_line("colleague", cfg) == ""


# ── what never leaves ────────────────────────────────────────────────────────
def test_password_managers_are_dropped_without_being_configured():
    ex = desktop._excludes(Cfg())
    for app in ("keepass", "1password", "bitwarden", "lastpass"):
        assert desktop._blocked(f"{app}.exe", "vault", ex)


def test_exclusions_match_the_title_as_well_as_the_app():
    ex = desktop._excludes(Cfg(desktop_exclude=["salary"]))
    assert desktop._blocked("excel.exe", "Salary review 2026.xlsx", ex)
    assert not desktop._blocked("excel.exe", "Budget.xlsx", ex)


def test_an_excluded_window_never_enters_the_snapshot(monkeypatch):
    """Dropped where it is read, so there is no path where it exists as context."""
    monkeypatch.setattr(desktop, "_windows_win32",
                        lambda ex: (None, [w for w in
                                           [{"app": "keepass.exe", "title": "vault"},
                                            {"app": "excel.exe", "title": "Budget.xlsx"}]
                                           if not desktop._blocked(w["app"], w["title"], ex)]))
    monkeypatch.setattr(desktop.platform, "system", lambda: "Windows")
    snap = desktop.snapshot(Cfg())
    titles = [w["title"] for w in snap["windows"]]
    assert titles == ["Budget.xlsx"]


def test_nothing_is_written_to_disk():
    """The screen is a live signal. A record of it would need retention,
    erasure and export like everything else here."""
    src = inspect.getsource(desktop)
    for forbidden in ("open(", "sqlite3", "json.dump", "write_text", "Path("):
        assert forbidden not in src, f"desktop.py persists something ({forbidden})"


def test_the_history_is_short_and_in_memory(fake_screen):
    fake_screen([{"app": "a.exe", "title": "one"}])
    cfg = Cfg(desktop_awareness=True, desktop_owner="u1", desktop_sample_s=0)
    for _ in range(30):
        desktop.sample(cfg)
    assert len(desktop._ring) <= desktop._RING_MAX


# ── what the model is told ───────────────────────────────────────────────────
def test_the_note_is_background_and_says_not_to_recite_it(fake_screen):
    fake_screen([{"app": "excel.exe", "title": "Budget.xlsx"}])
    line = desktop.context_line("u1", Cfg(desktop_awareness=True, desktop_owner="u1"))
    assert line.startswith("[Background") and line.endswith("]")
    assert "not" in line.lower() and "quote or mention" in line.lower()
    assert "they did not ask about it" in line.lower()


def test_the_focused_window_leads(fake_screen):
    fake_screen([{"app": "excel.exe", "title": "Budget.xlsx"},
                 {"app": "chrome.exe", "title": "Gmail"}],
                focused={"app": "excel.exe", "title": "Budget.xlsx"})
    line = desktop.context_line("u1", Cfg(desktop_awareness=True, desktop_owner="u1"))
    assert line.index("Budget.xlsx") < line.index("Gmail")
    assert "in front of them" in line


def test_an_empty_screen_produces_no_note(fake_screen):
    fake_screen([])
    assert desktop.context_line("u1", Cfg(desktop_awareness=True, desktop_owner="u1")) == ""


def test_long_titles_are_trimmed(fake_screen):
    fake_screen([{"app": "x.exe", "title": "y" * 500}])
    line = desktop.context_line("u1", Cfg(desktop_awareness=True, desktop_owner="u1"))
    assert len(line) < 700 and "…" in line


# ── the wiring ───────────────────────────────────────────────────────────────
def test_the_turn_replaces_the_note_rather_than_stacking_it():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "suni/core/orchestrator.py").read_text(encoding="utf-8")
    i = src.index("context_line(user_id")
    block = src[i - 400:i + 400]
    assert 'm.agent != "desktop"' in block, "yesterday's windows would pile up in context"


def test_it_reaches_the_path_this_install_actually_uses():
    """force_claude_code sends turns to the CLI; background that only reaches
    the local tiers reaches nothing here."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "suni/models/claude_code_agent.py").read_text(encoding="utf-8")
    assert '"desktop"' in src
