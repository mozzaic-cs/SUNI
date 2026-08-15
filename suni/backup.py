"""
SUNI backup and restore.

Creates timestamped ZIP archives of all critical state files.
Restores by extracting to the correct paths and prompting restart.

Backup directory: backups/ (created automatically)
Filename format:  SUNI_backup_YYYY-MM-DD_HH-MM-SS.zip
"""
from __future__ import annotations
import json
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
            for lf in sorted(Path("logs").glob("suni_*.log")):
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


def restore(zip_path: Path) -> dict:
    """
    Restore files from a backup ZIP.
    Returns {"restored": [...], "errors": [...], "meta": {...}}.
    A SUNI restart is required after restore.
    """
    if not zip_path.exists():
        raise FileNotFoundError(f"Backup not found: {zip_path}")

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if "backup_manifest.json" not in names:
            raise ValueError("Not a valid SUNI backup — missing backup_manifest.json")

        meta = json.loads(zf.read("backup_manifest.json"))
        restored, errors = [], []

        for name in names:
            if name == "backup_manifest.json":
                continue
            try:
                dest = Path(name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(name))
                restored.append(name)
            except Exception as e:
                errors.append(f"{name}: {e}")

    return {"restored": restored, "errors": errors, "meta": meta}


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
