"""
Subject access (GDPR Art 15/20) — and the guard that keeps it honest.

Two failure modes are worth more than the happy path.

**Drift.** If export and erasure enumerate stores separately, one eventually
covers a table the other does not, and the subject is told something untrue
either way. They share `erasure.SUBJECT_TABLES`, so the test that matters is not
"do they agree" — that would compare a constant to itself — but "does the
inventory cover every user-keyed table that actually exists", asked by walking
sqlite_master.

**Credential leakage.** The export is a file that leaves the machine. Columns are
allow-listed, so a credential added upstream is absent by default; the tests
check both the allowlist and, separately, that no known secret VALUE appears
anywhere in the serialised payload — the second catches a nested leak the first
cannot see.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from suni import erasure, subject_access

from tests.test_erasure import SUBJECT, OTHER, instance  # noqa: F401


ROOT = Path(__file__).resolve().parent.parent

# Column names that mean "this row belongs to a person".
_USER_COLUMNS = {"user_id", "owner_id"}


# ── the inventory must cover reality ─────────────────────────────────────────
def test_every_user_keyed_table_is_in_the_inventory(instance):
    """Walks sqlite_master. A user-keyed table added upstream fails here rather
    than escaping both erasure and export unnoticed."""
    listed = {(db, t) for db, t, _c, _l in erasure.SUBJECT_TABLES}
    listed |= {(db, t) for db, t, *_ in erasure.JOIN_TABLES}
    listed.add((erasure.AUDIT_TABLE[0], erasure.AUDIT_TABLE[1]))

    missing = []
    for db_path in sorted((instance / "memory").glob("*.db")):
        with sqlite3.connect(str(db_path)) as c:
            for (table,) in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"):
                cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
                keyed = bool(cols & _USER_COLUMNS) or (
                    table == "users" and "id" in cols)
                if keyed and (db_path.name, table) not in listed:
                    missing.append(f"{db_path.name}::{table}")
    assert not missing, f"user-keyed tables outside the inventory: {missing}"


def test_the_inventory_covers_the_real_instance_databases():
    """The same guard, pointed at memory/ rather than at a fixture.

    This is the one that earns its keep. The fixture only contains databases the
    tests themselves create, so it passed while the live instance had four
    user-keyed tables nobody had listed — bg_tasks, watch_items, projects and
    project_members. Erasure and export both silently skipped them.

    Skips when there is no instance data (CI, a fresh clone), which is why it
    supplements the fixture test rather than replacing it.
    """
    mem = ROOT / "memory"
    dbs = sorted(mem.glob("*.db")) if mem.is_dir() else []
    if not dbs:
        pytest.skip("no instance databases in this checkout")

    listed = {(db, t) for db, t, _c, _l in erasure.SUBJECT_TABLES}
    listed |= {(db, t) for db, t, *_ in erasure.JOIN_TABLES}
    listed.add((erasure.AUDIT_TABLE[0], erasure.AUDIT_TABLE[1]))

    missing = []
    for db_path in dbs:
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as c:
                for (table,) in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'"):
                    cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
                    keyed = bool(cols & _USER_COLUMNS) or (
                        table == "users" and "id" in cols)
                    if keyed and (db_path.name, table) not in listed:
                        missing.append(f"{db_path.name}::{table}")
        except sqlite3.Error:
            continue        # a locked or partial db is not this test's business
    assert not missing, (
        "user-keyed tables in the live instance that erasure and export both "
        f"miss: {missing}")


def test_every_inventory_table_has_export_columns():
    """Adding a table to the inventory without an allowlist would erase it but
    export nothing from it — a silent asymmetry."""
    for _db, table, _c, _l in erasure.SUBJECT_TABLES:
        assert table in subject_access.EXPORT_COLUMNS, f"{table} has no allowlist"


def test_shared_membership_tables_never_export_the_user_column():
    """agent_members and project_members name OTHER people."""
    for table in ("agent_members", "project_members"):
        cols = subject_access.EXPORT_COLUMNS[table]
        assert "user_id" not in cols, f"{table} would export other members' ids"


def test_the_join_table_is_not_invisible_to_that_guard():
    """`messages` has no user column, so a naive 'user-keyed' rule skips the one
    table that already caused a bug. It is listed explicitly."""
    joined = {t for _db, t, *_ in erasure.JOIN_TABLES}
    assert "messages" in joined


def test_export_and_erasure_read_the_same_inventory():
    """Not a tautology check — a structural one: subject_access must import the
    inventory rather than declare its own."""
    src = (ROOT / "suni" / "subject_access.py").read_text(encoding="utf-8")
    assert "from .erasure import" in src
    assert "SUBJECT_TABLES" in src and "JOIN_TABLES" in src
    assert "SUBJECT_TABLES: " not in src, "subject_access declares a second inventory"


def test_every_exported_table_has_an_allowlist():
    for _db, table, _c, _l in erasure.SUBJECT_TABLES:
        assert table in subject_access.EXPORT_COLUMNS, \
            f"{table} is erased but has no export allowlist"
    for _db, table, *_ in erasure.JOIN_TABLES:
        assert table in subject_access.EXPORT_COLUMNS


# ── credentials must not leave ───────────────────────────────────────────────
def test_no_credential_columns_are_allow_listed():
    flat = {c for cols in subject_access.EXPORT_COLUMNS.values() for c in cols}
    for banned in ("password_h", "api_token_h", "anthropic_api_key", "smtp_pass"):
        assert banned not in flat, f"{banned} would be exported"


def test_the_encrypted_settings_keys_are_not_exported():
    from suni import user_settings
    for key in user_settings._ENCRYPTED_KEYS:
        assert key not in subject_access.SETTINGS_KEYS


def test_no_secret_value_appears_anywhere_in_the_payload(instance):
    """The allowlist check is per-column; this one catches a nested leak."""
    with sqlite3.connect(instance / "memory" / "users.db") as c:
        c.execute("UPDATE users SET password_h=?, api_token_h=? WHERE id=?",
                  ("HASH-CANARY-9times", "TOKEN-CANARY-9times", SUBJECT))
    udir = instance / "memory" / "users" / SUBJECT
    (udir / "settings.json").write_text(json.dumps({
        "stt_language": "pt-PT",
        "anthropic_api_key": "APIKEY-CANARY-9times",
        "smtp_pass": "SMTPPASS-CANARY-9times",
    }), encoding="utf-8")

    blob = subject_access.to_json(subject_access.export(SUBJECT, root=instance))
    for canary in ("HASH-CANARY", "TOKEN-CANARY", "APIKEY-CANARY", "SMTPPASS-CANARY"):
        assert canary not in blob, f"{canary} leaked into the export"
    assert "pt-PT" in blob, "the non-secret settings were dropped too"


def test_an_unknown_column_added_upstream_is_not_exported(instance):
    """The allowlist fails closed: a new column is absent until listed."""
    with sqlite3.connect(instance / "memory" / "users.db") as c:
        c.execute("ALTER TABLE users ADD COLUMN recovery_secret TEXT")
        c.execute("UPDATE users SET recovery_secret='NEWCOL-CANARY' WHERE id=?",
                  (SUBJECT,))
    blob = subject_access.to_json(subject_access.export(SUBJECT, root=instance))
    assert "NEWCOL-CANARY" not in blob


# ── other people stay out ────────────────────────────────────────────────────
def test_other_members_of_a_shared_agent_are_not_exported(instance):
    with sqlite3.connect(instance / "memory" / "agents.db") as c:
        c.execute("INSERT INTO agent_members VALUES ('a1',?,'member','')", (OTHER,))
    blob = subject_access.to_json(subject_access.export(SUBJECT, root=instance))
    assert OTHER not in blob, "another user's id is in this subject's export"


def test_another_users_conversations_are_not_exported(instance):
    payload = subject_access.export(SUBJECT, root=instance)
    ids = {c["id"] for c in payload["data"]["conversations"]}
    assert ids == {"c1", "c2"}
    for m in payload["data"]["messages"]:
        assert m["conversation_id"] in ids


# ── content ──────────────────────────────────────────────────────────────────
def test_the_export_contains_the_subjects_own_data(instance):
    p = subject_access.export(SUBJECT, root=instance)
    assert p["subject"]["username"] == "alex"
    d = p["data"]
    assert len(d["conversations"]) == 2 and len(d["messages"]) == 6
    assert len(d["audit_log"]) == 2
    assert len(d["schedules"]) == 1 and len(d["agents_owned"]) == 1
    assert any("Privet Drive" in e.get("content", "") for e in d["personal_memory"])


def test_embedding_vectors_are_stripped(instance):
    udir = instance / "memory" / "users" / SUBJECT
    (udir / "suni_memory.json").write_text(json.dumps(
        [{"id": "m1", "content": "hello", "e16": "BASE64VECTORCANARY", "metadata": {}}]),
        encoding="utf-8")
    blob = subject_access.to_json(subject_access.export(SUBJECT, root=instance))
    assert "BASE64VECTORCANARY" not in blob
    assert "hello" in blob


def test_it_states_what_it_could_not_include(instance):
    """Same limitation, same words as the erasure preview — a store that cannot
    be attributed to one person cannot be handed to them either."""
    p = subject_access.export(SUBJECT, root=instance)
    stores = [u["store"] for u in p["not_included"]]
    assert any("suni_memory.json" in s for s in stores)
    erase_stores = [u["store"] for u in
                    erasure.preview(SUBJECT, root=instance)["not_erasable"]]
    assert stores == erase_stores, "export and erasure describe different limits"


def test_export_is_read_only(instance):
    from tests.test_erasure import _subject_hits
    before = _subject_hits(instance, SUBJECT)
    subject_access.export(SUBJECT, root=instance)
    assert _subject_hits(instance, SUBJECT) == before


def test_it_serialises(instance):
    json.loads(subject_access.to_json(subject_access.export(SUBJECT, root=instance)))


def test_a_missing_user_yields_an_empty_export_not_a_crash(instance):
    p = subject_access.export("no-such-user", root=instance)
    assert p["subject"]["username"] == ""
    assert p["data"]["conversations"] == []


# ── the endpoint ─────────────────────────────────────────────────────────────
def test_the_export_endpoint_is_admin_only_and_audited():
    srv = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    i = srv.index('@app.get("/api/users/{user_id}/export")')
    block = srv[i:i + 1200]
    assert "require_admin" in block
    assert "log_event" in block, "the most sensitive payload in the system is unaudited"
    assert "attachment" in block


def test_nothing_mails_or_ships_the_export():
    """It is a download. Sending it somewhere is the admin's decision, not a
    feature that quietly exists."""
    srv = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    i = srv.index('@app.get("/api/users/{user_id}/export")')
    block = srv[i:i + 1200]
    for bad in ("email_notify", "smtp", "log_ship", "requests.post"):
        assert bad not in block
