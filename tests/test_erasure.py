"""
Erasing a person from every store — GDPR Art 17.

The bug underneath the feature: `auth.delete_user()` deleted one row from
users.db and left the person's conversations, memory, schedules, agents and
audit trail behind. So the load-bearing test here is not "does erase() run" but
"is the subject actually gone from every database" — asked by walking
sqlite_master, so that a table added upstream later fails this instead of
quietly retaining data.

Two shapes get their own tests because both look like success while failing:

  messages have no user column, so deleting conversations by user_id strands
  every message body;

  MemoryStore._save() rewrites the whole file from memory, so erasing the JSON
  under a loaded store lets the next save resurrect it.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from suni import erasure


SUBJECT = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


@pytest.fixture()
def instance(tmp_path: Path) -> Path:
    """A miniature SUNI instance: every store, two users, data for both."""
    mem = tmp_path / "memory"
    mem.mkdir()

    with sqlite3.connect(mem / "users.db") as c:
        c.execute("CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT, "
                  "password_h TEXT, role TEXT, active INT, api_token_h TEXT, "
                  "created_at TEXT, last_login TEXT, auth_source TEXT)")
        c.execute("CREATE TABLE oidc_identities (issuer TEXT, sub TEXT, "
                  "user_id TEXT, created_at TEXT)")
        for uid, name in ((SUBJECT, "alex"), (OTHER, "sam")):
            c.execute("INSERT INTO users VALUES (?,?,'h','standard',1,'','','','local')",
                      (uid, name))
            c.execute("INSERT INTO oidc_identities VALUES ('iss',?,?,'')", (name, uid))

    with sqlite3.connect(mem / "conversations.db") as c:
        c.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, user_id TEXT, "
                  "title TEXT, created_at TEXT, updated_at TEXT, cc_session_id TEXT)")
        c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, "
                  "conversation_id TEXT, role TEXT, content TEXT, ts TEXT)")
        for cid, uid in (("c1", SUBJECT), ("c2", SUBJECT), ("c3", OTHER)):
            c.execute("INSERT INTO conversations VALUES (?,?,'t','','','')", (cid, uid))
            for i in range(3):
                c.execute("INSERT INTO messages (conversation_id, role, content, ts) "
                          "VALUES (?,'user',?,'')", (cid, f"secret-{cid}-{i}"))

    with sqlite3.connect(mem / "audit.db") as c:
        c.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, ts TEXT, "
                  "user_id TEXT, username TEXT, session_id TEXT, ip_address TEXT, "
                  "query_preview TEXT, route TEXT, mode TEXT, tools_called TEXT, "
                  "tool_errors TEXT, duration_s REAL, approved_by TEXT, "
                  "prompt_tokens INT, gen_tokens INT, agent_slug TEXT)")
        for uid, name in ((SUBJECT, "alex"), (SUBJECT, "alex"), (OTHER, "sam")):
            c.execute("INSERT INTO audit_log (ts, user_id, username, ip_address, "
                      "query_preview, route, mode, duration_s) VALUES "
                      "('2026-01-01',?,?,'10.0.0.7','what is my salary','chat','fast',1.5)",
                      (uid, name))

    with sqlite3.connect(mem / "schedules.db") as c:
        c.execute("CREATE TABLE schedules (id TEXT PRIMARY KEY, name TEXT, "
                  "owner_id TEXT, owner_name TEXT, prompt TEXT, agent_slug TEXT, "
                  "cadence TEXT, delivery TEXT, enabled INT, next_run TEXT, "
                  "last_run TEXT, last_status TEXT, run_count INT, created_at TEXT)")
        c.execute("INSERT INTO schedules VALUES ('s1','n',?,'alex','p','', "
                  "'daily','',1,'','','',0,'')", (SUBJECT,))

    with sqlite3.connect(mem / "agents.db") as c:
        c.execute("CREATE TABLE agents (slug TEXT PRIMARY KEY, name TEXT, "
                  "description TEXT, owner_id TEXT, model TEXT, mode TEXT, "
                  "tools_json TEXT, blocked_json TEXT, mcp_json TEXT, enabled INT, "
                  "created_at TEXT, updated_at TEXT, used_count INT, last_used TEXT, "
                  "max_steps INT, max_runs_day INT)")
        c.execute("CREATE TABLE agent_members (slug TEXT, user_id TEXT, role TEXT, "
                  "added_at TEXT)")
        c.execute("INSERT INTO agents VALUES ('a1','A','d',?,'m','','','','',1,"
                  "'','',0,'',0,0)", (SUBJECT,))
        c.execute("INSERT INTO agent_members VALUES ('a2',?,'member','')", (SUBJECT,))

    udir = mem / "users" / SUBJECT
    udir.mkdir(parents=True)
    (udir / "suni_memory.json").write_text(
        json.dumps([{"id": "m1", "content": "alex lives at 4 Privet Drive",
                     "type": "fact", "metadata": {}}]), encoding="utf-8")
    (mem / "suni_memory.json").write_text(
        json.dumps([{"id": "g1", "content": "shared", "metadata": {}}]), encoding="utf-8")
    return tmp_path


# ── the load-bearing check ───────────────────────────────────────────────────
def _subject_hits(root: Path, subject: str) -> dict[str, int]:
    """Every row in every table of every database that still mentions the
    subject — found by walking sqlite_master, not a hand-written list."""
    hits: dict[str, int] = {}
    for db in sorted((root / "memory").glob("*.db")):
        with sqlite3.connect(str(db)) as c:
            tables = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")]
            for t in tables:
                cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
                where = " OR ".join(f"CAST({col} AS TEXT)=?" for col in cols)
                n = c.execute(f"SELECT COUNT(*) FROM {t} WHERE {where}",
                              tuple([subject] * len(cols))).fetchone()[0]
                if n:
                    hits[f"{db.name}::{t}"] = n
    return hits


def test_the_subject_is_gone_from_every_table(instance):
    before = _subject_hits(instance, SUBJECT)
    assert before, "the fixture never stored the subject anywhere"
    erasure.erase(SUBJECT, SUBJECT, root=instance)
    after = _subject_hits(instance, SUBJECT)
    # audit_log deliberately keeps user_id — see the module docstring
    assert set(after) <= {"audit.db::audit_log"}, f"subject survives in {after}"


def test_a_table_added_later_would_fail_this():
    """The sweep reads sqlite_master rather than a fixed list, so a new
    user-keyed table is caught instead of silently retaining data."""
    import inspect
    src = inspect.getsource(_subject_hits)
    assert "sqlite_master" in src and "PRAGMA table_info" in src


# ── the two failure shapes that look like success ────────────────────────────
def test_messages_are_deleted_through_their_conversation(instance):
    """messages has no user column. Deleting conversations by user_id alone
    leaves every message body behind, orphaned and unreachable."""
    erasure.erase(SUBJECT, SUBJECT, root=instance)
    with sqlite3.connect(instance / "memory" / "conversations.db") as c:
        left = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        bodies = [r[0] for r in c.execute("SELECT content FROM messages")]
    assert left == 3, "the other user's messages were destroyed or the subject's kept"
    assert all("c3" in b for b in bodies), f"subject message bodies survive: {bodies}"


def test_a_live_store_cannot_resurrect_erased_memory(instance):
    """MemoryStore._save() rewrites the file from its in-memory list, so a
    cached store saving after the erasure would write everything back."""
    class FakeStore:
        def __init__(self):
            self.cleared = False
        def clear(self):
            self.cleared = True
    store = FakeStore()
    erasure.erase(SUBJECT, SUBJECT, root=instance, memory_store=store)
    assert store.cleared, "the live store was never cleared — a later save undoes this"


def test_the_personal_memory_directory_is_removed(instance):
    d = instance / "memory" / "users" / SUBJECT
    assert d.exists()
    erasure.erase(SUBJECT, SUBJECT, root=instance)
    assert not d.exists()


# ── the audit trail is kept, but stripped ────────────────────────────────────
def test_audit_rows_are_pseudonymised_not_deleted(instance):
    """Art 17(3)(b) vs AI Act Art 26(6): the log must survive, and it is also
    the evidence that the erasure happened."""
    erasure.erase(SUBJECT, SUBJECT, root=instance)
    with sqlite3.connect(instance / "memory" / "audit.db") as c:
        rows = c.execute("SELECT username, ip_address, query_preview, route, "
                         "duration_s FROM audit_log WHERE user_id=?",
                         (SUBJECT,)).fetchall()
    assert len(rows) == 2, "the audit rows were deleted rather than pseudonymised"
    for username, ip, preview, route, dur in rows:
        assert username == "" and ip == "" and preview == ""
        assert route == "chat" and dur == 1.5, "operational columns were destroyed too"


def test_another_users_audit_rows_are_untouched(instance):
    erasure.erase(SUBJECT, SUBJECT, root=instance)
    with sqlite3.connect(instance / "memory" / "audit.db") as c:
        row = c.execute("SELECT username, ip_address FROM audit_log WHERE user_id=?",
                        (OTHER,)).fetchone()
    assert row == ("sam", "10.0.0.7")


# ── blast radius: only the subject ───────────────────────────────────────────
def test_the_other_user_survives_completely(instance):
    erasure.erase(SUBJECT, SUBJECT, root=instance)
    assert _subject_hits(instance, OTHER), "erasing one user removed another"
    with sqlite3.connect(instance / "memory" / "users.db") as c:
        assert c.execute("SELECT COUNT(*) FROM users WHERE id=?", (OTHER,)).fetchone()[0] == 1


# ── preview ──────────────────────────────────────────────────────────────────
def test_preview_touches_nothing(instance):
    before = _subject_hits(instance, SUBJECT)
    erasure.preview(SUBJECT, root=instance)
    assert _subject_hits(instance, SUBJECT) == before


def test_preview_counts_what_erase_removes(instance):
    p = erasure.preview(SUBJECT, root=instance)
    assert p["exists"] is True and p["username"] == "alex"
    e = p["erasable"]
    assert e["conversations"] == 2 and e["messages"] == 6
    assert e["schedules"] == 1 and e["agents_owned"] == 1
    assert e["agent_memberships"] == 1 and e["account"] == 1
    assert e["personal_memory_entries"] == 1
    assert p["pseudonymised"]["audit_rows"] == 2


def test_preview_reports_what_cannot_be_erased(instance):
    """An operator who believes erasure was complete when it was not is worse
    off than one who knows what remains."""
    p = erasure.preview(SUBJECT, root=instance)
    stores = [u["store"] for u in p["not_erasable"]]
    assert any("suni_memory.json" in s for s in stores), \
        "the unattributed global store is not reported"
    assert any("doc_" in s for s in stores)
    for u in p["not_erasable"]:
        assert u["reason"], "a limitation is listed with no explanation"


def test_backups_are_reported_and_never_touched(instance):
    (instance / "backups").mkdir()
    (instance / "backups" / "snap.zip").write_text("x", encoding="utf-8")
    erasure.erase(SUBJECT, SUBJECT, root=instance)
    assert (instance / "backups" / "snap.zip").exists()
    p = erasure.preview(SUBJECT, root=instance)
    assert "snap.zip" in p["backups"]
    assert "re-introduces" in p["backups_note"]


# ── confirmation ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("confirm", ["", "wrong-id", OTHER])
def test_erase_refuses_without_the_echoed_id(instance, confirm):
    erasure.erase(SUBJECT, confirm, root=instance)
    assert _subject_hits(instance, SUBJECT), "data was erased without confirmation"


def test_the_endpoint_requires_admin_and_the_echoed_id():
    root = Path(__file__).resolve().parent.parent
    srv = (root / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    i = srv.index('@app.post("/api/users/{user_id}/erase")')
    block = srv[i:i + 1800]
    assert "require_admin" in block
    assert "confirm_user_id" in block, "a bare POST could erase a person"
    assert "_user_memories.pop" in block, \
        "the cached memory manager would write the erased entries back"


def test_nothing_schedules_erasure():
    """It is admin-triggered by design. An automatic caller would be a
    data-destroying background job."""
    root = Path(__file__).resolve().parent.parent
    srv = (root / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    i = srv.index("async def _schedule_runner")
    from tests.test_updater import _function_body
    assert "erase(" not in _function_body(srv, "async def _schedule_runner")
    assert "erasure" not in srv[i:i + 200].lower()
