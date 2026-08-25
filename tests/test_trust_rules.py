"""
"Always allow" has to mean always.

The checkbox writes a per-user trust rule that lets a consequential tool run
without the approval gate. Those rules lived in a module-level dict, so they
were gone at the next restart — while the UI said "Always allow this tool", and
the Portuguese UI said "Permitir sempre". A permission granted this morning and
silently withdrawn by an overnight restart teaches people to click the gate
rather than read it, which is the opposite of what the gate is for.

They now persist to memory/users/{user_id}/trust_rules.json, which puts them
inside the directory erasure already removes wholesale.

The path is relative, matching user_settings, so these tests chdir into a tmp
directory rather than writing into the developer's real store.
"""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from suni import approval


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """A private working directory and an empty cache for every test."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(approval, "_trust_rules", {})
    monkeypatch.setattr(approval, "_trust_loaded", set())
    return tmp_path


USER = "7ea12e5c-c9eb-4935-9a3c-a0e380dfca3d"


def _restart():
    """Simulate a process restart: the cache goes, the file stays."""
    approval._trust_rules.clear()
    approval._trust_loaded.clear()


# ── the bug ──────────────────────────────────────────────────────────────────
def test_a_rule_survives_a_restart(isolated_store):
    approval.add_trust_rule(USER, "send_email")
    assert approval.is_trusted(USER, "send_email", {}) is True

    _restart()
    assert approval.is_trusted(USER, "send_email", {}) is True, \
        "the rule did not survive — this is the bug the file exists to prevent"


def test_the_rule_is_written_where_erasure_looks(isolated_store):
    approval.add_trust_rule(USER, "send_email")
    path = isolated_store / "memory" / "users" / USER / "trust_rules.json"
    assert path.is_file(), "not inside the per-user directory erasure removes"
    assert json.loads(path.read_text(encoding="utf-8")) == {"send_email": ["*"]}


def test_trust_is_still_per_user(isolated_store):
    approval.add_trust_rule(USER, "send_email")
    _restart()
    assert approval.is_trusted("someone-else", "send_email", {}) is False


def test_trust_is_still_per_tool(isolated_store):
    approval.add_trust_rule(USER, "send_email")
    _restart()
    assert approval.is_trusted(USER, "run_shell", {}) is False


# ── revocation ───────────────────────────────────────────────────────────────
def test_revoking_survives_a_restart_too(isolated_store):
    """The dangerous asymmetry: a granted rule that persists and a revocation
    that does not would resurrect the permission at the next restart."""
    approval.add_trust_rule(USER, "send_email")
    approval.remove_trust_rule(USER, "send_email")
    assert approval.is_trusted(USER, "send_email", {}) is False

    _restart()
    assert approval.is_trusted(USER, "send_email", {}) is False


def test_revoking_the_last_pattern_drops_the_tool(isolated_store):
    """An empty list would still list the tool in the settings dialog, so the
    user would see a permission they had already revoked."""
    approval.add_trust_rule(USER, "send_email")
    approval.remove_trust_rule(USER, "send_email")
    assert approval.list_trust_rules(USER) == {}
    _restart()
    assert approval.list_trust_rules(USER) == {}


def test_listing_reads_from_disk(isolated_store):
    """The settings dialog calls list_trust_rules through /api/approval/trust.
    If listing did not load, the dialog would show nothing to revoke while the
    rules were still being honoured."""
    approval.add_trust_rule(USER, "send_email")
    _restart()
    assert approval.list_trust_rules(USER) == {"send_email": ["*"]}


# ── the file is untrusted input ──────────────────────────────────────────────
def test_a_corrupt_file_grants_nothing(isolated_store):
    path = isolated_store / "memory" / "users" / USER / "trust_rules.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert approval.is_trusted(USER, "send_email", {}) is False


@pytest.mark.parametrize("payload", [
    '"send_email"',                 # not a mapping
    '{"send_email": "*"}',          # patterns not a list
    '{"send_email": [1, 2]}',       # patterns not strings
    '{"send_email": []}',           # empty: a tool with no pattern is no rule
])
def test_malformed_entries_grant_nothing(isolated_store, payload):
    path = isolated_store / "memory" / "users" / USER / "trust_rules.json"
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")
    assert approval.is_trusted(USER, "send_email", {}) is False


def test_a_user_id_cannot_escape_the_store(isolated_store):
    """user_id arrives from an authenticated session, but it becomes a path
    segment here, so it is checked rather than trusted."""
    for hostile in ("../../etc", "a/b", "..", "C:/windows", ""):
        assert approval._trust_path(hostile) is None
        approval.add_trust_rule(hostile, "run_shell")   # must not raise
    assert not list(isolated_store.rglob("trust_rules.json")), \
        "a rejected id still wrote a file somewhere"


def test_a_rejected_id_still_works_in_memory(isolated_store):
    """Refusing to persist must not silently refuse to apply — that would be a
    permission the user granted and watched do nothing."""
    approval.add_trust_rule("a/b", "run_shell")
    assert approval.is_trusted("a/b", "run_shell", {}) is True


# ── cost ─────────────────────────────────────────────────────────────────────
def test_the_file_is_read_once_not_per_call(isolated_store, monkeypatch):
    """is_trusted runs on every gated dispatch. Reading the file each time would
    put disk I/O in the tool-call path."""
    approval.add_trust_rule(USER, "send_email")
    _restart()

    reads = []
    real = Path.read_text
    def counting(self, *a, **kw):
        if self.name == "trust_rules.json":
            reads.append(str(self))
        return real(self, *a, **kw)
    monkeypatch.setattr(Path, "read_text", counting)

    for _ in range(20):
        approval.is_trusted(USER, "send_email", {})
    assert len(reads) == 1, f"read the store {len(reads)} times"


# ── erasure ──────────────────────────────────────────────────────────────────
def test_forget_user_clears_the_cache(isolated_store):
    """Erasure deletes the directory, but a long-running server would keep
    honouring the cached rules until restart."""
    approval.add_trust_rule(USER, "send_email")
    approval.forget_user(USER)
    # The file is gone in a real erasure; here it still exists, so a cache that
    # was not cleared and a cache that reloaded look the same. Remove it too.
    (isolated_store / "memory" / "users" / USER / "trust_rules.json").unlink()
    assert approval.is_trusted(USER, "send_email", {}) is False


def test_erasure_forgets_trust_rules(isolated_store, monkeypatch):
    """The inventory in erasure.py must reach this store, not just the files it
    already knew about."""
    from suni import erasure
    called = []
    monkeypatch.setattr(approval, "forget_user", lambda uid: called.append(uid))
    # erase() requires the subject's id echoed back before it does anything.
    erasure.erase(USER, USER, root=isolated_store)
    assert called == [USER], "erase() did not clear the trust-rule cache"


def test_an_unconfirmed_erasure_forgets_nothing(isolated_store, monkeypatch):
    """Guard the guard: if erase() cleared the cache before checking the
    confirmation, the test above would pass on a function that erases nothing."""
    from suni import erasure
    called = []
    monkeypatch.setattr(approval, "forget_user", lambda uid: called.append(uid))
    result = erasure.erase(USER, "wrong-id", root=isolated_store)
    assert result["ok"] is False
    assert called == []
