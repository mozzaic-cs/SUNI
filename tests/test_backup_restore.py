"""
Restore is staged on upload and applied at the next start, never written over
a running server.

Writing straight over the live files was measured on throwaway copies
(2026-10-05) to do three things, each reproduced below against synthetic data:

  * a database the server held in WAL mode came back with the LIVE rows: the
    connection's log was written back over the restored file, and restore()
    reported success with no errors;
  * a restored memory file was overwritten by the server's next save from the
    copy in RAM;
  * an archive entry named "../x" was written outside the SUNI directory.

Everything here runs in a temporary directory: backup.py works relative to the
current directory, as the server does.
"""
from __future__ import annotations
import json
import socket
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

from suni import backup


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "memory").mkdir()
    return tmp_path


def _db(path, rows, wal=True):
    c = sqlite3.connect(path)
    if wal:
        c.execute("pragma journal_mode=wal")
    c.execute("create table if not exists t(v text)")
    c.executemany("insert into t values(?)", [(r,) for r in rows])
    c.commit()
    return c


def _rows(path):
    c = sqlite3.connect(path)
    try:
        assert c.execute("pragma integrity_check").fetchone()[0] == "ok"
        return [r[0] for r in c.execute("select v from t")]
    finally:
        c.close()


def _backup_now():
    return backup.BACKUP_DIR / backup.create()


def _zip(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("backup_manifest.json", json.dumps({"created_at": "x"}))
        for n, b in entries.items():
            zf.writestr(n, b)
    return path


# ── the case that read as success ────────────────────────────────────────────
def test_a_restore_brings_back_the_backup_not_the_live_wal(root):
    _db("memory/conversations.db", ["from-backup"]).close()
    z = _backup_now()

    # the running state: newer rows, and a -wal the way a killed server leaves it
    live = _db("memory/conversations.db", [f"live-{i}" for i in range(20)])
    wal = Path("memory/conversations.db-wal")
    assert wal.exists()
    stale = wal.read_bytes()
    live.close()                      # a clean close checkpoints and removes the -wal...
    wal.write_bytes(stale)            # ...a killed process does not

    before = (Path("memory/conversations.db").read_bytes(), wal.read_bytes())
    staged = backup.stage_restore(z, by="admin")
    assert "memory/conversations.db" in staged["files"]
    # compared as bytes: OPENING the database would replay and remove the -wal
    assert (Path("memory/conversations.db").read_bytes(), wal.read_bytes()) == before, \
        "staging touched live data"

    out = backup.apply_pending()
    assert out["status"] == "applied", out
    assert _rows("memory/conversations.db") == ["from-backup"]
    aside = Path(out["previous_files"])
    assert (aside / "memory/conversations.db-wal").exists(), "the stale -wal was not moved aside"
    assert not Path("memory/conversations.db-wal").exists()
    assert backup.pending_restore() is None
    assert backup.last_restore()["status"] == "applied"


def test_a_restored_memory_file_is_what_the_next_start_loads(root):
    Path("memory/suni_memory.json").write_text('{"entries": ["from-backup"]}', encoding="utf-8")
    z = _backup_now()
    Path("memory/suni_memory.json").write_text('{"entries": ["newer"]}', encoding="utf-8")
    backup.stage_restore(z)
    assert "newer" in Path("memory/suni_memory.json").read_text(encoding="utf-8")
    backup.apply_pending()
    assert "from-backup" in Path("memory/suni_memory.json").read_text(encoding="utf-8")


# ── nothing outside what a backup can hold ───────────────────────────────────
@pytest.mark.parametrize("name", [
    "../escaped.txt",
    "memory/../../escaped.txt",
    "/abs/escaped.txt",
    "C:/escaped.txt",
    "memory\\..\\escaped.txt",
    "suni/web/server.py",             # a real path, but not one a backup writes
    "memory/users/a/b/settings.json", # '*' is one folder, not several
])
def test_an_entry_outside_what_a_backup_holds_refuses_the_whole_archive(root, name):
    Path("memory/role_config.json").write_text("{}", encoding="utf-8")
    z = _zip(root / "crafted.zip", {"memory/role_config.json": b"{}", name: b"x"})
    with pytest.raises(ValueError, match="Refused"):
        backup.stage_restore(z)
    assert backup.pending_restore() is None
    assert not (root.parent / "escaped.txt").exists()
    assert not (backup.BACKUP_DIR / "restore_pending.tmp").exists()


def test_everything_create_writes_is_restorable(root):
    """The allowlist is built from create()'s own constants; a backup it made
    must never be refused."""
    for f in ("memory/suni_config.json", "memory/mcp_servers.json"):
        Path(f).write_text("{}", encoding="utf-8")
    _db("memory/users.db", ["u"]).close()
    for f in ("memory/users/u-1/settings.json", "memory/users/u-1/suni_memory.json",
              "memory/skills/writer/SKILL.md", "memory/skills/writer/refs/a.md",
              "memory/uploads/u-1/doc.pdf", "logs/suni_2026-10-05.log",
              "memory/doc_index.faiss"):
        Path(f).parent.mkdir(parents=True, exist_ok=True)
        Path(f).write_text("x", encoding="utf-8")
    z = backup.BACKUP_DIR / backup.create(include_faiss=True, include_logs=True, include_uploads=True)
    staged = backup.stage_restore(z)
    assert len(staged["files"]) == len(zipfile.ZipFile(z).namelist()) - 1


def test_a_damaged_database_is_refused(root):
    _db("memory/users.db", ["u"], wal=False).close()
    z = _zip(root / "bad.zip", {"memory/users.db": b"SQLite format 3\x00" + b"\x00" * 200})
    with pytest.raises(ValueError, match="integrity"):
        backup.stage_restore(z)
    assert backup.pending_restore() is None


# ── when it is applied ───────────────────────────────────────────────────────
def test_nothing_is_applied_while_suni_is_running(root):
    _db("memory/users.db", ["old"], wal=False).close()
    z = _backup_now()
    _db("memory/users.db", ["new"], wal=False).close()
    backup.stage_restore(z)
    with socket.socket() as srv:                     # something on the port
        srv.bind(("127.0.0.1", 0)); srv.listen(1)
        out = backup.apply_pending(port=srv.getsockname()[1])
    assert out["status"] == "deferred"
    assert _rows("memory/users.db") == ["old", "new"]
    assert backup.pending_restore() is not None, "a deferred restore must stay staged"


@pytest.mark.skipif(sys.platform != "win32", reason="on Linux renaming an open file succeeds")
def test_a_file_still_held_open_rolls_everything_back(root):
    _db("memory/users.db", ["old"], wal=False).close()
    _db("memory/contacts.db", ["c-old"], wal=False).close()
    z = _backup_now()
    held = _db("memory/contacts.db", ["c-new"], wal=False)    # still open
    try:
        out = backup.apply_pending() if backup.stage_restore(z) else None
    finally:
        held.close()
    assert out["status"] == "rolled_back", out
    assert _rows("memory/users.db") == ["old"]
    assert _rows("memory/contacts.db") == ["c-old", "c-new"]
    assert backup.pending_restore() is not None


def test_an_older_backup_says_what_it_does_not_cover(root):
    _db("memory/users.db", ["u"], wal=False).close()
    z = _backup_now()
    _db("memory/projects.db", ["p"], wal=False).close()      # created after the backup
    staged = backup.stage_restore(z)
    assert "memory/projects.db" in staged["not_in_backup"]
    backup.apply_pending()
    assert _rows("memory/projects.db") == ["p"], "a file the backup lacks must be left alone"


def test_a_staged_restore_can_be_cancelled(root):
    _db("memory/users.db", ["u"], wal=False).close()
    backup.stage_restore(_backup_now())
    assert backup.cancel_pending() is True
    assert backup.pending_restore() is None
    assert backup.apply_pending() is None


def test_web_py_applies_it_before_any_other_suni_import():
    src = (Path(__file__).resolve().parent.parent / "web.py").read_text(encoding="utf-8")
    at = src.index("apply_pending(")
    for imp in ("from suni.logger", "from suni.system_profile", "from suni.web.server"):
        assert src.index(imp) > at, f"{imp} runs before the restore is applied"
    assert src.rfind('if __name__ == "__main__":', 0, at) != -1, \
        "importing web.py (tests, tools) must not apply a restore"


def test_the_old_write_in_place_restore_is_gone():
    assert not hasattr(backup, "restore")
