"""
Routing ingested Claude Code sessions to an owner who can actually be erased.

The watcher reads ~/.claude/projects/ and stores what it finds. Those
transcripts are personal data, but the ingest path has no signed-in user, so
everything landed in the shared store with no attribution — which is exactly
why erasure and subject access report that store as out of reach.

`session_ingest_owner` routes NEW ingests into that user's own memory, where
both operations already work. The tests that matter:

  the setting reaches the watcher — a config key that nothing reads is this
  codebase's most repeated defect, and it looks identical to a working feature;

  it is re-read every cycle, so changing the owner does not need a restart;

  it fails SAFE — an unknown or disabled owner falls back to the shared store
  and logs, rather than dropping the transcripts on the floor.
"""
from __future__ import annotations

import asyncio
import inspect
import pathlib

import pytest

from suni.ingestion import watcher

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_key_exists_and_defaults_to_the_old_behaviour():
    from suni import config
    assert config.DEFAULTS["session_ingest_owner"] == "", \
        "defaulting to an owner would silently reassign someone's transcripts"


# ── the setting must actually reach the watcher ──────────────────────────────
def test_the_watcher_accepts_a_resolver():
    assert "resolve" in inspect.signature(watcher.watch).parameters


def test_the_server_passes_the_resolver_in():
    """Without this line the config key would be read by nothing — the shape of
    every 'setting that reaches nothing' bug in this project."""
    srv = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    assert "resolve=_ingest_target" in srv, "the watcher still writes to the shared store"
    assert "def _ingest_target" in srv
    i, j = srv.index("def _ingest_target"), srv.index("resolve=_ingest_target")
    assert i < j, "_ingest_target is used before it is defined"


def test_the_resolver_reads_the_config_key():
    srv = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    i = srv.index("def _ingest_target")
    block = srv[i:i + 1600]
    assert "session_ingest_owner" in block
    assert "_get_user_memory" in block, "it never routes to the user's own store"


def test_an_unknown_or_inactive_owner_falls_back_and_says_so():
    """Dropping the ingest would lose the transcripts entirely; failing silently
    would look like it worked."""
    srv = (ROOT / "suni" / "web" / "server.py").read_text(encoding="utf-8-sig")
    i = srv.index("def _ingest_target")
    block = srv[i:i + 1600]
    assert "active" in block and "_log.warning" in block
    assert "return memory" in block


# ── behaviour of the watch loop itself ───────────────────────────────────────
class _Stub:
    def __init__(self, name):
        self.name = name


def _run_one_cycle(monkeypatch, resolve, sessions=("s1",)):
    """Drive exactly one watcher pass and report which store it ingested into."""
    used = []

    async def fake_ingest(manager):
        used.append(manager)
        return {"chunks": 1, "sessions": 1}

    monkeypatch.setattr(watcher, "_load_state", lambda: {})
    monkeypatch.setattr(watcher, "find_new_or_updated", lambda state: list(sessions))
    monkeypatch.setattr(watcher, "ingest_all", fake_ingest)

    async def drive():
        stop = asyncio.Event()
        task = asyncio.create_task(watcher.watch(_Stub("shared"), stop, resolve=resolve))
        for _ in range(50):
            await asyncio.sleep(0)
            if used:
                break
        stop.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return used

    return asyncio.run(drive())


def test_the_resolver_decides_the_target(monkeypatch):
    owned = _Stub("alex-store")
    used = _run_one_cycle(monkeypatch, resolve=lambda: owned)
    assert used and used[0] is owned, "the watcher ignored the resolver"


def test_without_a_resolver_it_uses_the_shared_store(monkeypatch):
    """The CLI calls watch() with no resolver and must be unaffected."""
    used = _run_one_cycle(monkeypatch, resolve=None)
    assert used and used[0].name == "shared"


def test_the_resolver_is_re_asked_every_cycle():
    """Resolved once at startup, changing the owner would need a restart — and
    it would also pin a store that erasure has since deleted."""
    src = inspect.getsource(watcher.watch)
    i = src.index("while not stop_event")
    assert "resolve()" in src[i:], "the resolver is called outside the loop"


# ── the picker in the admin panel ────────────────────────────────────────────
def test_the_select_is_populated_before_the_value_is_applied():
    """Assigning a <select>.value with no matching <option> silently leaves it
    on the first entry — a configured owner would read back as 'shared' and the
    next save would clear it."""
    html = (ROOT / "suni" / "web" / "admin.html").read_text(encoding="utf-8")
    i = html.index("async function loadConfig")
    body = html[i:i + 2500]
    pop = body.index("session_ingest_owner")
    apply_at = body.index("applyConfigToForm(cfg)")
    assert pop < apply_at, "the picker is filled after the config value is set"


def test_the_field_is_inside_the_config_form():
    html = (ROOT / "suni" / "web" / "admin.html").read_text(encoding="utf-8")
    i = html.index('id="cfg-form"')
    j = html.index("</form>", i)
    assert 'name="session_ingest_owner"' in html[i:j], \
        "the picker is outside cfg-form and would never save"


def test_the_hint_says_what_the_default_costs():
    """An operator choosing the default should know it puts the data beyond
    erasure, not just that it is 'shared'."""
    js = (ROOT / "suni" / "web" / "i18n.js").read_text(encoding="utf-8")
    i = js.index('"admin.hint_ingest_owner"')
    block = js[i:i + 700]
    assert "erasure" in block and "export" in block
