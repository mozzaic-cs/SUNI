"""
Shared fixtures for the SUNI test suite.

Isolation strategy:
  - os.chdir(tmpdir) redirects all relative file paths (SQLite DBs,
    FAISS index, JSON stores) to a throw-away temp directory.
  - Orchestrator._safe_run is patched to return a canned string so tests
    never need a live Ollama or Claude Code process.
  - Three test users (admin / standard / read-only) are created once per
    session and their JWT tokens reused across all tests.
"""
from __future__ import annotations
import os
import json
import pytest
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Filesystem isolation
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def suni_tmpdir(tmp_path_factory):
    d = tmp_path_factory.mktemp("suni_test")
    (d / "memory" / "users").mkdir(parents=True)
    return d


@pytest.fixture(scope="session", autouse=True)
def isolate_filesystem(suni_tmpdir):
    """Change CWD to tmpdir for the whole session so all relative SUNI paths
    (memory/*.db, memory/*.json, memory/*.faiss) land in the temp dir."""
    orig = os.getcwd()
    os.chdir(suni_tmpdir)
    yield
    os.chdir(orig)


# ---------------------------------------------------------------------------
# Test users
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def test_users(isolate_filesystem):
    import suni.auth as auth
    auth.init_db()   # create schema in tmpdir/memory/users.db
    u = {
        "admin":    auth.create_user("admin_test",   "Admin123!",    "admin"),
        "standard": auth.create_user("std_test",     "Standard123!", "standard"),
        "readonly": auth.create_user("ro_test",      "Readonly123!", "read-only"),
    }
    return u


# ---------------------------------------------------------------------------
# App + client
# ---------------------------------------------------------------------------

async def _mock_safe_run(self, user_input, context, **kwargs):
    return "ok"


@pytest.fixture(scope="session")
def app(test_users):
    patcher = patch(
        "suni.core.orchestrator.Orchestrator._safe_run",
        new=_mock_safe_run,
    )
    patcher.start()

    from suni.web.server import create_app
    _app = create_app()

    yield _app

    patcher.stop()


@pytest.fixture(scope="session")
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Auth header helpers
# ---------------------------------------------------------------------------

def _get_token(client, username: str, password: str) -> dict:
    r = client.post("/api/auth/login",
                    data={"username": username, "password": password})
    assert r.status_code == 200, f"Login failed for {username}: {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="session")
def admin_headers(client, test_users):
    return _get_token(client, "admin_test", "Admin123!")


@pytest.fixture(scope="session")
def std_headers(client, test_users):
    return _get_token(client, "std_test", "Standard123!")


@pytest.fixture(scope="session")
def ro_headers(client, test_users):
    return _get_token(client, "ro_test", "Readonly123!")


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def parse_sse(body: bytes) -> list[dict]:
    """Parse a raw SSE response body into a list of event data dicts."""
    events = []
    for line in body.decode("utf-8", errors="replace").splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events
