"""suni/overlay.py: a private add-on folder is optional and can never break SUNI."""
from __future__ import annotations

import textwrap

import pytest

from suni import overlay


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    """The overlay module pointed at an empty folder, as on a stranger's clone."""
    monkeypatch.setattr(overlay, "OVERLAY_DIR", tmp_path)
    monkeypatch.setattr(overlay, "PACKAGE", "suni_overlay_test_pkg")
    monkeypatch.setattr(overlay, "_module", None)
    monkeypatch.setattr(overlay, "_tried", False)
    yield tmp_path
    import sys
    sys.modules.pop("suni_overlay_test_pkg", None)
    if str(tmp_path) in sys.path:
        sys.path.remove(str(tmp_path))


def _write_pkg(root, body: str) -> None:
    pkg = root / "suni_overlay_test_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(textwrap.dedent(body), encoding="utf-8")


def test_without_the_folder_everything_is_a_no_op(fresh):
    assert overlay.active() is False
    assert overlay.page_script() == ""
    assert overlay.subject_tables() == ()
    assert overlay.export_columns() == {}
    overlay.on_turn({"source": "web"})
    overlay.after_upkeep_scan()
    overlay.mount(object(), {})


def test_an_add_on_that_raises_is_swallowed(fresh):
    _write_pkg(fresh, """
        def on_turn(info): raise RuntimeError("boom")
        def page_script(): raise RuntimeError("boom")
        def after_upkeep_scan(): raise RuntimeError("boom")
    """)
    assert overlay.active() is True
    overlay.on_turn({})
    overlay.after_upkeep_scan()
    assert overlay.page_script() == ""


def test_an_add_on_that_does_not_import_is_no_add_on(fresh):
    _write_pkg(fresh, "raise ImportError('broken')\n")
    assert overlay.active() is False


def test_add_on_tables_go_before_the_account_rows(fresh, monkeypatch):
    _write_pkg(fresh, """
        SUBJECT_TABLES = (("extra.db", "things", "user_id", "things"),)
        EXPORT_COLUMNS = {"things": ("at",)}
    """)
    from suni import erasure
    base = (("a.db", "t", "user_id", "t"), ("users.db", "users", "id", "account"))
    out = erasure._with_overlay(base)
    assert [r[1] for r in out] == ["t", "things", "users"]


def test_turn_source():
    from suni.core.orchestrator import _turn_source
    assert _turn_source("web", "some-uuid") == "web"
    assert _turn_source("", "telegram_12345") == "telegram"
    assert _turn_source("", "slack_C01") == "slack"
    # no explicit source and no channel prefix: a schedule, a background task,
    # an agent preview — nobody is waiting on it, so it is not a person's turn
    assert _turn_source("", "6b1f-uuid") == ""
    assert _turn_source("", "") == ""


def test_the_add_on_folder_is_never_tracked():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(["git", "ls-files", "personal"], cwd=str(root),
                         capture_output=True, text=True).stdout.strip()
    assert out == ""
    ignored = subprocess.run(["git", "check-ignore", "-q", "personal/x.py"], cwd=str(root))
    assert ignored.returncode == 0, "personal/ must be in .gitignore"
