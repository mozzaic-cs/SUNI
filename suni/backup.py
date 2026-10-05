"""
SUNI backup and restore.

Creates timestamped ZIP archives of all critical state files.

Restore is two steps, and the split is the point. Uploading a backup only
STAGES it: every entry is checked against what create() can write, every
database is integrity-checked, and the files are unpacked into
backups/restore_pending/. Nothing live is touched. The next start applies it
(web.py, before anything has opened a file), moving every file it replaces
into backups/pre_restore_<time>/ first, so it can be undone by hand.

It used to extract straight over the live files while the server ran. Measured
on throwaway copies (2026-10-05), that did three things:
  * a database the server held open in WAL mode came back with the LIVE rows:
    its connection wrote its log back over the restored file on close, and
    restore() reported success with no errors;
  * a restored memory file was overwritten by the server's next save, from the
    copy it still held in RAM;
  * an entry named "../x" was written outside the SUNI directory.

Backup directory: backups/ (created automatically)
Filename format:  SUNI_backup_YYYY-MM-DD_HH-MM-SS.zip
"""
from __future__ import annotations
import json
import os
import re
import shutil
import socket
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKUP_DIR = Path("backups")

# ── Always included: config, security, and all non-rebuildable user data ──
_CORE_FILES = [
    # config & security
    "memory/suni_config.json",
    "memory/role_config.json",
    "memory/api_key.txt",
    "memory/jwt_secret.txt",
    "memory/mcp_servers.json",
    # databases (user data)
    "memory/users.db",
    "memory/audit.db",
    "memory/conversations.db",
    "memory/skills.db",
    "memory/contacts.db",
    "memory/projects.db",
    "memory/monitor.db",
    "memory/bg_tasks.db",
    # memory stores
    "memory/suni_memory.json",
    "memory/collective_memory.json",
    # knowledge-base / ingestion metadata (the FAISS index itself is optional)
    "memory/doc_meta.json",
    "memory/doc_scan.json",
    "memory/ingestion_state.json",
    "memory/article_ingest_state.json",
    "memory/seen_email_uids.json",
    "memory/model_inventory.json",
]

# ── Glob patterns: per-user files + the skills tree (SKILL.md + seed sentinels) ──
_GLOB_PATTERNS = [
    "memory/users/*/suni_memory.json",
    "memory/users/*/suni_memory.meta.json",
    "memory/users/*/settings.json",
    "memory/skills/**/*",
]

# Large optional data — rebuildable or bulky, opt-in via flags.
_FAISS_INDEX  = "memory/doc_index.faiss"
_UPLOADS_GLOB = "memory/uploads/**/*"
_LOGS_GLOB    = "logs/suni_*.log"

# Restore staging: the upload lands here, the next start applies it.
_PENDING    = BACKUP_DIR / "restore_pending"
_READY      = "READY.json"          # written last: no READY, nothing to apply
_LAST       = BACKUP_DIR / "last_restore.json"
_SIDE_FILES = ("-wal", "-shm", "-journal")


def _sqlite_snapshot(db_path: Path) -> bytes:
    """Return a consistent snapshot of a live SQLite DB via the online backup
    API — this coexists safely with the running server's own connection, unlike
    a plain file copy which can capture a torn or stale WAL state. Uses a normal
    (read-write) source connection: a read-only URI connection can't create the
    -wal/-shm shared memory a live WAL database needs."""
    import sqlite3, tempfile, os
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(tmp)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
        return Path(tmp).read_bytes()
    finally:
        src.close()
        try:
            os.remove(tmp)
        except OSError:
            pass


def _add_file(zf: zipfile.ZipFile, path: Path, arcname: str) -> None:
    """Add one file to the archive. SQLite databases (.db) go in as a consistent
    online-backup snapshot; anything that isn't a valid/openable SQLite DB falls
    back to a byte-for-byte copy."""
    if arcname.endswith(".db"):
        try:
            zf.writestr(arcname, _sqlite_snapshot(path))
            return
        except Exception:
            pass  # not SQLite, or locked — plain copy below
    zf.write(path, arcname)


def create(include_faiss: bool = False, include_logs: bool = False,
           include_uploads: bool = False) -> str:
    """
    Create a backup ZIP. Returns the filename.
    include_faiss:   add doc_index.faiss (rebuildable; can be 10s–100s MB).
    include_logs:    add daily log files.
    include_uploads: add memory/uploads/ (user-uploaded source docs; can be large).
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts       = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"SUNI_backup_{ts}.zip"
    dest     = BACKUP_DIR / filename

    archived: list[str] = []
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # Core single files
        for f in _CORE_FILES:
            p = Path(f)
            if p.exists():
                _add_file(zf, p, f)
                archived.append(f)

        # Glob-matched files (per-user data + skills tree)
        for pattern in _GLOB_PATTERNS:
            for mp in Path(".").glob(pattern):
                if mp.is_file():
                    arc = mp.as_posix()
                    _add_file(zf, mp, arc)
                    archived.append(arc)

        # FAISS index (optional — large, rebuildable)
        if include_faiss:
            p = Path(_FAISS_INDEX)
            if p.exists():
                zf.write(p, _FAISS_INDEX)
                archived.append(_FAISS_INDEX)

        # User uploads (optional — can be large)
        if include_uploads:
            for up in Path(".").glob(_UPLOADS_GLOB):
                if up.is_file():
                    arc = up.as_posix()
                    zf.write(up, arc)
                    archived.append(arc)

        # Daily logs (optional — can be large)
        if include_logs:
            for lf in sorted(Path(".").glob(_LOGS_GLOB)):
                arc = lf.as_posix()
                zf.write(lf, arc)
                archived.append(arc)

        # Manifest
        meta: dict[str, Any] = {
            "created_at":       datetime.now(timezone.utc).isoformat(),
            "suni_version":     "2026-04-28",
            "includes_faiss":   include_faiss,
            "includes_logs":    include_logs,
            "includes_uploads": include_uploads,
            "files":            archived,
        }
        zf.writestr("backup_manifest.json", json.dumps(meta, indent=2))

    return filename


def list_backups() -> list[dict]:
    """List available backups, newest first."""
    BACKUP_DIR.mkdir(exist_ok=True)
    out = []
    for f in sorted(BACKUP_DIR.glob("SUNI_backup_*.zip"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        st = f.stat()
        out.append({
            "filename":   f.name,
            "size_kb":    round(st.st_size / 1024, 1),
            "created_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        })
    return out


# ── restore ──────────────────────────────────────────────────────────────────

def _glob_re(pattern: str) -> re.Pattern:
    """A glob as create() means it: '*' stays inside one folder, '**/' spans
    any number of them."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:[^/]+/)*"; i += 3
        elif pattern[i] == "*":
            out += "[^/]*"; i += 1
        else:
            out += re.escape(pattern[i]); i += 1
    return re.compile(out + r"\Z")


_RESTORABLE = [_glob_re(p) for p in
               (*_GLOB_PATTERNS, _UPLOADS_GLOB, _LOGS_GLOB)]


def _restorable(name: str) -> bool:
    """True only for a path create() could have written. Anything else in an
    uploaded archive is refused, so a crafted backup cannot write elsewhere."""
    if not name or name.startswith(("/", "\\")) or "\\" in name or ":" in name:
        return False
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    if name in _CORE_FILES or name == _FAISS_INDEX:
        return True
    return any(r.match(name) for r in _RESTORABLE)


def _db_ok(path: Path) -> bool:
    try:
        c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            return c.execute("pragma integrity_check").fetchone()[0] == "ok"
        finally:
            c.close()
    except Exception:
        return False


def _live_files() -> list[str]:
    """What a backup made now would hold, so a restore can say which live
    files the archive does not cover (an older backup holds fewer)."""
    out = [f for f in _CORE_FILES if Path(f).is_file()]
    for pattern in _GLOB_PATTERNS:
        out += [p.as_posix() for p in Path(".").glob(pattern) if p.is_file()]
    return out


def stage_restore(zip_path: Path, by: str = "") -> dict:
    """Check a backup and unpack it into restore_pending/. Touches nothing live.

    Refuses the whole archive (writing nothing) if any entry is outside what a
    backup can contain, or any database fails its integrity check.
    """
    if not zip_path.exists():
        raise FileNotFoundError(f"Backup not found: {zip_path}")
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if "backup_manifest.json" not in names:
            raise ValueError("Not a valid SUNI backup — missing backup_manifest.json")
        bad_crc = zf.testzip()
        if bad_crc:
            raise ValueError(f"The archive is damaged: {bad_crc} fails its checksum")
        files = [n for n in names if n != "backup_manifest.json"]
        refused = [n for n in files if not _restorable(n)]
        if refused:
            raise ValueError("Refused: the archive holds paths a SUNI backup never "
                             "contains: " + ", ".join(refused[:10]))
        if not files:
            raise ValueError("The archive holds no files to restore")
        meta = json.loads(zf.read("backup_manifest.json"))

        tmp = BACKUP_DIR / "restore_pending.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        root = tmp.resolve()
        try:
            for n in files:
                dest = (tmp / n).resolve()
                if not dest.is_relative_to(root):          # belt and braces
                    raise ValueError(f"Refused: {n} resolves outside the staging area")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(n))
            broken = [n for n in files if n.endswith(".db") and not _db_ok(tmp / n)]
            if broken:
                raise ValueError("Refused: these databases fail their integrity "
                                 "check: " + ", ".join(broken))
            live = set(_live_files())
            info = {
                "staged_at":  datetime.now(timezone.utc).isoformat(),
                "staged_by":  by,
                "source":     zip_path.name,
                "created_at": meta.get("created_at"),
                "files":      files,
                # live files this backup does not hold: left as they are
                "not_in_backup": sorted(live - set(files)),
            }
            (tmp / _READY).write_text(json.dumps(info, indent=2), encoding="utf-8")
            shutil.rmtree(_PENDING, ignore_errors=True)
            tmp.rename(_PENDING)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    return {"staged": True, **info}


def pending_restore() -> dict | None:
    """The staged restore waiting for a restart, or None."""
    try:
        return json.loads((_PENDING / _READY).read_text(encoding="utf-8"))
    except Exception:
        return None


def cancel_pending() -> bool:
    if not _PENDING.exists():
        return False
    shutil.rmtree(_PENDING, ignore_errors=True)
    return not _PENDING.exists()


def last_restore() -> dict | None:
    """What the last start did with a staged restore."""
    try:
        return json.loads(_LAST.read_text(encoding="utf-8"))
    except Exception:
        return None


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.settimeout(0.5)
        return sk.connect_ex(("127.0.0.1", port)) == 0


def apply_pending(port: int | None = None) -> dict | None:
    """Apply a staged restore. Call at start-up, BEFORE anything opens a file
    under memory/. Returns the outcome (also written to last_restore.json), or
    None when nothing was staged.

    All or nothing: every file being replaced is moved aside first, with its
    -wal/-shm/-journal (a stale WAL left beside a restored database would be
    replayed into it). If any move fails, everything moved goes back and the
    restore stays staged. On Windows a failed move is also how a file still
    held open shows up; on Linux a rename of an open file succeeds, so the
    port check is what keeps this away from a running instance.
    """
    info = pending_restore()
    if info is None:
        return None
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out: dict[str, Any] = {"at": datetime.now(timezone.utc).isoformat(),
                           "source": info.get("source"),
                           "created_at": info.get("created_at"),
                           "files": len(info.get("files", [])),
                           "not_in_backup": info.get("not_in_backup", [])}

    def _done(result: dict) -> dict:
        try:
            _LAST.write_text(json.dumps(result, indent=2), encoding="utf-8")
        except Exception:
            pass
        return result

    if port and _port_in_use(port):
        return _done({**out, "status": "deferred",
                      "reason": f"something is already listening on port {port}; "
                                "a restore is only applied while SUNI is stopped"})

    aside = BACKUP_DIR / f"pre_restore_{ts}"
    moved: list[tuple[Path, Path]] = []        # (live, aside)
    placed: list[tuple[Path, Path]] = []       # (live, staged)
    try:
        for n in info["files"]:
            for suffix in ("", *_SIDE_FILES):
                live = Path(n + suffix)
                if live.exists():
                    a = aside / (n + suffix)
                    a.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(live, a)
                    moved.append((live, a))
        for n in info["files"]:
            live, staged = Path(n), _PENDING / n
            live.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, live)
            placed.append((live, staged))
    except Exception as e:
        # undo in reverse: the staged files back to staging, the originals home
        for live, staged in reversed(placed):
            try:
                os.replace(live, staged)
            except Exception:
                pass
        for live, a in reversed(moved):
            try:
                os.replace(a, live)
            except Exception:
                pass
        if not any(a.exists() for _, a in moved):    # everything went home
            shutil.rmtree(aside, ignore_errors=True)
        return _done({**out, "status": "rolled_back",
                      "reason": f"{type(e).__name__}: {e}",
                      "note": "nothing was changed; the restore is still staged"})
    shutil.rmtree(_PENDING, ignore_errors=True)
    return _done({**out, "status": "applied",
                  "previous_files": aside.as_posix() if moved else None})


def delete(filename: str) -> bool:
    """Delete a backup. Returns True if it existed and was deleted."""
    p = BACKUP_DIR / filename
    if p.exists() and p.name.startswith("SUNI_backup_") and p.suffix == ".zip":
        p.unlink()
        return True
    return False


def prune(keep: int = 10) -> int:
    """Delete oldest backups beyond keep_count. Returns count deleted."""
    files = sorted(BACKUP_DIR.glob("SUNI_backup_*.zip"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    to_delete = files[keep:]
    for f in to_delete:
        f.unlink()
    return len(to_delete)
