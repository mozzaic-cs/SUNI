"""
Erasing everything SUNI holds about one person — GDPR Art 17.

This exists because `auth.delete_user()` deletes a row from `users.db` and
nothing else. The account disappeared; the person's conversations, memory,
schedules, agents and audit trail stayed exactly where they were, now with no
account to connect them to. "Delete user" was already a promise the code did not
keep, so this is as much a data-integrity fix as a compliance feature.

Three things are handled differently on purpose.

**The audit trail is pseudonymised, not deleted.** Art 17(3)(b) does not require
erasure where processing is needed for a legal obligation, and AI Act Art 26(6)
is one — a deployer of a high-risk system keeps its logs for at least six
months. So the identifying columns (`username`, `ip_address`, `query_preview`)
are cleared while `ts`, `route`, `mode` and durations remain. `user_id` stays
too: once the account row is gone it is an opaque uuid that resolves to nobody,
and keeping it is what lets the operational record stay coherent. Deleting the
rows outright would break the retention obligation AND destroy the evidence that
the erasure happened.

**Conversation messages are reached through their conversation.** The `messages`
table has no user column — it keys on `conversation_id`. Deleting by user_id
alone removes the conversations and leaves every message body orphaned and
unreachable, which looks like success and is the opposite of it.

**Some stores cannot be erased per-person, and say so.** The global
`memory/suni_memory.json` carries no user attribution on any entry, and the
document index keys on file paths from shared locations. Neither can be filtered
by subject. They are reported in the preview as findings rather than skipped
quietly, because an operator who believes erasure was complete when it was not
is worse off than one who knows exactly what remains.

Nothing here runs on a timer. Erasure is admin-triggered, always preview first,
and `erase()` refuses unless the caller echoes back the subject's id.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# The audit columns that identify a natural person. Everything else in
# audit_log is operational and stays.
_AUDIT_IDENTIFYING = ("username", "ip_address", "query_preview")

# ── Where one person's data lives ────────────────────────────────────────────
# The single inventory that erasure AND subject access both read. Two modules
# each keeping their own list is how you end up with an export that omits a
# store erasure deletes, or the reverse — and either one is a lie told to a data
# subject. tests/test_erasure.py walks sqlite_master and fails if a user-keyed
# table exists that is not listed here, so adding a table upstream breaks the
# test rather than silently escaping both operations.
#
# (database, table, column holding the subject's id, label)
# Order matters for deletion: the account row goes last, so a failure partway
# through never leaves rows whose owner has already been removed.
SUBJECT_TABLES: tuple[tuple[str, str, str, str], ...] = (
    ("schedules.db", "schedules", "owner_id", "schedules"),
    ("agents.db", "agents", "owner_id", "agents_owned"),
    ("agents.db", "agent_members", "user_id", "agent_memberships"),
    ("conversations.db", "conversations", "user_id", "conversations"),
    # These four were missed by the first version of this module. The unit
    # fixture only contained the databases the tests created, so the guard
    # passed while the real instance had four user-keyed tables nobody had
    # listed — background task titles and results, monitor watch values, and
    # project goals and action logs are all the subject's own content.
    # tests now run the same sqlite_master check against the REAL memory/
    # directory, which is what actually caught this.
    ("bg_tasks.db", "bg_tasks", "user_id", "background_tasks"),
    ("monitor.db", "watch_items", "user_id", "monitor_watches"),
    ("projects.db", "project_members", "user_id", "project_memberships"),
    ("projects.db", "projects", "user_id", "projects_owned"),
    ("users.db", "oidc_identities", "user_id", "oidc_identities"),
    ("users.db", "users", "id", "account"),
)

# Tables with NO user column, reachable only through a parent. `messages` is the
# whole reason this constant exists: keyed on conversation_id, it is invisible to
# any "WHERE user_id=?" sweep, so deleting the parent first strands every message
# body. Listed explicitly so the sqlite_master guard can see it too.
#
# (database, table, fk column, parent table, parent key, parent's user column, label)
JOIN_TABLES: tuple[tuple[str, str, str, str, str, str, str], ...] = (
    ("conversations.db", "messages", "conversation_id",
     "conversations", "id", "user_id", "messages"),
)

# The audit table is neither erased nor ignored — it is pseudonymised in place,
# so it is tracked separately from both tuples above.
AUDIT_TABLE = ("audit.db", "audit_log", "user_id")


def _db(root: Path, name: str) -> Path:
    return root / "memory" / name


def _count(path: Path, sql: str, args: tuple = ()) -> int:
    if not path.exists():
        return 0
    try:
        with sqlite3.connect(str(path)) as c:
            row = c.execute(sql, args).fetchone()
            return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def _personal_memory_dir(root: Path, user_id: str) -> Path:
    return root / "memory" / "users" / user_id


def _personal_memory_count(root: Path, user_id: str) -> int:
    """Entries in the subject's own store, without loading an embedding model."""
    import json
    p = _personal_memory_dir(root, user_id) / "suni_memory.json"
    if not p.exists():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        entries = data.get("entries", data)
        return len(entries) if hasattr(entries, "__len__") else 0
    return 0


def unattributed_stores(root: Path) -> list[dict[str, Any]]:
    """Stores that hold content but cannot be filtered by subject.

    Reported every time, including when the counts are zero, because the point
    is to tell the operator what erasure does NOT cover.

    Shared with subject access, deliberately: a store that cannot be erased for
    one person cannot be exported for them either, and the two operations must
    describe the same limitation in the same words or one of them is wrong.
    """
    import json
    out: list[dict[str, Any]] = []

    # Counts only the entries that genuinely cannot be attributed. Since org
    # extraction began stamping provenance.source_user_id, part of the shared
    # store IS reachable, and reporting the whole file as unerasable would be a
    # claim that gets less true every week.
    for name, label in (("suni_memory.json", "memory/suni_memory.json"),
                        ("collective_memory.json", "memory/collective_memory.json")):
        path = root / "memory" / name
        n = 0
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                entries = data if isinstance(data, list) else data.get("entries", [])
                n = sum(1 for e in entries
                        if not ((e.get("metadata") or {})
                                .get("provenance", {}) or {}).get("source_user_id"))
            except (OSError, ValueError):
                n = -1
        if n == 0 and not path.exists():
            continue
        out.append({
            "store": label,
            "entries": n,
            "reason": ("entries here that carry no provenance cannot be tied to — "
                       "or separated from — one person. Entries stamped with a "
                       "source user ARE erased; this count is what remains. "
                       "Review by hand if the subject may appear in it."),
        })

    doc_meta = root / "memory" / "doc_meta.json"
    dn = 0
    if doc_meta.exists():
        try:
            data = json.loads(doc_meta.read_text(encoding="utf-8"))
            dn = len(data) if hasattr(data, "__len__") else 0
        except (OSError, ValueError):
            dn = -1
    out.append({
        "store": "memory/doc_meta.json + doc_index.faiss",
        "entries": dn,
        "reason": ("the document index keys on file paths in shared locations, "
                   "not on users. Erase the source documents instead; the index "
                   "rebuilds from them."),
    })
    return out


def _backups(root: Path) -> list[str]:
    d = root / "backups"
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_file())


def preview(user_id: str, root: Path | None = None) -> dict[str, Any]:
    """Count what erase() would remove. Touches nothing.

    Building this first is what makes the confirmation step mean something: an
    admin who cannot see the blast radius is not consenting to it.
    """
    root = root or ROOT
    users_db = _db(root, "users.db")
    conv_db = _db(root, "conversations.db")
    audit_db = _db(root, "audit.db")

    username = ""
    if users_db.exists():
        try:
            with sqlite3.connect(str(users_db)) as c:
                row = c.execute("SELECT username FROM users WHERE id=?",
                                (user_id,)).fetchone()
                username = row[0] if row else ""
        except sqlite3.Error:
            username = ""

    erasable: dict[str, Any] = {}
    for db_name, table, column, label in SUBJECT_TABLES:
        erasable[label] = _count(
            _db(root, db_name), f"SELECT COUNT(*) FROM {table} WHERE {column}=?",
            (user_id,))
    for db_name, table, fk, parent, pkey, pcol, label in JOIN_TABLES:
        erasable[label] = _count(
            _db(root, db_name),
            f"SELECT COUNT(*) FROM {table} WHERE {fk} IN "
            f"(SELECT {pkey} FROM {parent} WHERE {pcol}=?)", (user_id,))
    erasable["personal_memory_entries"] = _personal_memory_count(root, user_id)
    return {
        "user_id": user_id,
        "username": username,
        "exists": bool(erasable["account"]),
        "erasable": erasable,
        "pseudonymised": {
            "audit_rows": _count(
                audit_db, "SELECT COUNT(*) FROM audit_log WHERE user_id=?", (user_id,)),
            "columns_cleared": list(_AUDIT_IDENTIFYING),
            "why": ("kept, with identifying columns cleared: AI Act Art 26(6) "
                    "requires the log to survive, and it is also the record that "
                    "this erasure took place"),
        },
        "personal_memory_dir": str(_personal_memory_dir(root, user_id)),
        "not_erasable": unattributed_stores(root),
        "backups": _backups(root),
        "backups_note": ("backups are never modified — restoring one re-introduces "
                         "this data. Delete the snapshots separately if required."),
    }


def erase(user_id: str, confirm_user_id: str, root: Path | None = None,
          memory_store: Any = None) -> dict[str, Any]:
    """Erase the subject. Requires the caller to echo back the subject's id.

    `memory_store` is the LIVE MemoryStore for this user if the process holds
    one. It matters: MemoryStore._save() rewrites the whole file from its
    in-memory list, so deleting the JSON underneath a loaded store means the
    next save resurrects every entry. Clearing the live object first makes the
    erasure stick.
    """
    root = root or ROOT
    result: dict[str, Any] = {"ok": False, "user_id": user_id, "deleted": {},
                              "pseudonymised": 0, "errors": [], "message": ""}
    if not user_id or confirm_user_id != user_id:
        result["message"] = ("erasure not confirmed: the subject's id must be "
                             "echoed back exactly")
        return result

    plan = preview(user_id, root)
    result["preview"] = plan

    # 1. The live store first — see the docstring. If this process holds the
    #    subject's memory, clearing the object is what prevents a later save
    #    from writing the deleted entries back out.
    if memory_store is not None:
        try:
            memory_store.clear()
        except Exception as exc:                     # noqa: BLE001
            result["errors"].append(f"live memory store: {exc}")

    # 2. Audit: clear the identifying columns, keep the row.
    audit_db = _db(root, "audit.db")
    if audit_db.exists():
        try:
            with sqlite3.connect(str(audit_db)) as c:
                sets = ", ".join(f"{col}=''" for col in _AUDIT_IDENTIFYING)
                n = c.execute(f"UPDATE audit_log SET {sets} WHERE user_id=?",
                              (user_id,)).rowcount
                result["pseudonymised"] = max(n, 0)
        except sqlite3.Error as exc:
            result["errors"].append(f"audit: {exc}")

    # 3. Child rows BEFORE their parents — they are only reachable through the
    #    parent, so deleting the parent first strands them permanently.
    for db_name, table, fk, parent, pkey, pcol, label in JOIN_TABLES:
        path = _db(root, db_name)
        if not path.exists():
            continue
        try:
            with sqlite3.connect(str(path)) as c:
                n = c.execute(
                    f"DELETE FROM {table} WHERE {fk} IN "
                    f"(SELECT {pkey} FROM {parent} WHERE {pcol}=?)",
                    (user_id,)).rowcount
                result["deleted"][label] = max(n, 0)
        except sqlite3.Error as exc:
            result["errors"].append(f"{table}: {exc}")

    # 4. The straightforward per-user rows, account last.
    for db_name, table, column, label in SUBJECT_TABLES:
        path = _db(root, db_name)
        if not path.exists():
            continue
        try:
            with sqlite3.connect(str(path)) as c:
                n = c.execute(f"DELETE FROM {table} WHERE {column}=?",
                              (user_id,)).rowcount
                result["deleted"][label] = max(n, 0)
        except sqlite3.Error as exc:
            result["errors"].append(f"{table}: {exc}")

    # 5. Shared-store entries the subject can be shown to have produced.
    #    Org extraction stamps provenance.source_user_id, so these ARE
    #    attributable even though the store as a whole is not. Skipping them
    #    would leave a person's words in memory their colleagues can read,
    #    after being told the erasure was done.
    for name in ("collective_memory.json", "suni_memory.json"):
        path = root / "memory" / name
        if not path.exists():
            continue
        try:
            import json as _json
            data = _json.loads(path.read_text(encoding="utf-8"))
            entries = data if isinstance(data, list) else data.get("entries", [])
            keep = [e for e in entries
                    if ((e.get("metadata") or {}).get("provenance", {}) or {})
                    .get("source_user_id") != user_id]
            removed = len(entries) - len(keep)
            if removed:
                # Whole-file rewrite, matching how MemoryStore persists. Any
                # process holding this store open will overwrite this on its
                # next save — the same live-store hazard as the personal store
                # above, which is why the endpoint evicts before calling here.
                path.write_text(_json.dumps(keep), encoding="utf-8")
                result["deleted"][f"shared_{name.split('_')[0]}_entries"] = removed
        except (OSError, ValueError) as exc:
            result["errors"].append(f"{name}: {exc}")

    # 6. The subject's own memory directory, including the sidecar meta and
    #    per-user settings that live alongside it.
    mem_dir = _personal_memory_dir(root, user_id)
    if mem_dir.is_dir():
        try:
            shutil.rmtree(mem_dir)
            result["deleted"]["personal_memory_dir"] = str(mem_dir)
        except OSError as exc:
            result["errors"].append(f"personal memory dir: {exc}")

    # 7. Approval trust rules are cached in the running process, so deleting
    #    trust_rules.json above is only half of it — a live server would keep
    #    honouring the erased subject's standing permissions until restart.
    try:
        from . import approval as _approval
        _approval.forget_user(user_id)
    except Exception as exc:                      # noqa: BLE001 — never block erasure
        result["errors"].append(f"trust rules: {exc}")

    result["ok"] = not result["errors"]
    result["message"] = _summarise(result)
    return result


def _summarise(result: dict[str, Any]) -> str:
    d = result["deleted"]
    bits = [f"Erased {result['user_id']}."]
    counts = ", ".join(f"{k}: {v}" for k, v in d.items() if isinstance(v, int) and v)
    if counts:
        bits.append(f"Deleted — {counts}.")
    if result["pseudonymised"]:
        bits.append(f"{result['pseudonymised']} audit row(s) kept with identifying "
                    f"columns cleared.")
    left = [u["store"] for u in result.get("preview", {}).get("not_erasable", [])]
    if left:
        bits.append("NOT covered: " + ", ".join(left) + ".")
    if result.get("preview", {}).get("backups"):
        bits.append("Backups were not modified.")
    if result["errors"]:
        bits.append("Errors: " + "; ".join(result["errors"]))
    return " ".join(bits)
