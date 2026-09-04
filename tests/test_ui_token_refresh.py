"""
An interface left open overnight must still work in the morning.

Access tokens last 8 hours. The Face page is the AMBIENT interface — it is meant
to be left running on a screen for days — so a token expiring there is the
normal case, not an edge case. `chat.html` refreshed on 401 from the day it was
written; `face.html` and `ui.html` never did. They attached the token, fetched,
and did nothing at all with a 401, so after eight hours every call failed
silently and the interface went inert.

It looks exactly like the server being down, and it was read that way: on
2026-09-04 SUNI was reported down while the process was healthy, listening,
answering, three days into an uninterrupted run — the tab in front of the
reporter had simply been holding a dead token since the day before.

The endpoint and the stored refresh token were both already there. Nobody was
calling them.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
UIS = ("suni/web/face.html", "suni/web/ui.html", "suni/web/chat.html")


def _fetch_wrapper(rel: str) -> str:
    """The body of the page's _apiFetch, which every API call goes through."""
    src = (ROOT / rel).read_text(encoding="utf-8")
    m = re.search(r"(?:async\s+)?function _apiFetch\b.*?\n\}", src, re.S)
    assert m, f"{rel} has no _apiFetch"
    return m.group(0)


# ── every UI, not just the one that happened to get it right ────────────────

@pytest.mark.parametrize("rel", UIS)
def test_the_ui_refreshes_instead_of_dying_on_401(rel):
    """The bug was not that refreshing is hard — it is that two of the three
    pages never attempted it."""
    body = _fetch_wrapper(rel)
    assert "401" in body, f"{rel} ignores 401 entirely"
    assert "/api/auth/refresh" in body, f"{rel} never tries to refresh"
    assert "suni_refresh_token" in body, f"{rel} does not read the refresh token"


@pytest.mark.parametrize("rel", UIS)
def test_the_original_request_is_replayed_after_a_refresh(rel):
    """Refreshing and then NOT retrying would still show the user a failure."""
    body = _fetch_wrapper(rel)
    assert body.count("fetch(") >= 3, (
        f"{rel} does not look like fetch -> refresh -> retry")
    assert "access_token" in body, f"{rel} never installs the new token"


@pytest.mark.parametrize("rel", UIS)
def test_it_only_sends_the_user_to_login_when_the_refresh_also_failed(rel):
    """Redirecting on the first 401 would throw away a session that a refresh
    would have rescued, which for the ambient page means losing the display
    every eight hours."""
    body = _fetch_wrapper(rel)
    idx_refresh = body.index("/api/auth/refresh")
    idx_login = body.index("/login")
    assert idx_refresh < idx_login, \
        f"{rel} redirects to login before trying to refresh"


@pytest.mark.parametrize("rel", ("suni/web/face.html", "suni/web/ui.html"))
def test_the_token_variable_can_actually_be_replaced(rel):
    """It was `const _ST`. A refresh that cannot write the new token back is a
    refresh that fixes exactly one call and then fails forever after."""
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "const _ST = localStorage" not in src, \
        f"{rel} still declares _ST as const; the refreshed token cannot be stored"
    assert re.search(r"let _ST = localStorage", src), f"{rel} lost its _ST declaration"


# ── the mechanism the pages now depend on ───────────────────────────────────

def test_refresh_returns_a_working_access_token(client, test_users):
    """End to end against the real endpoint: the UI fix is worthless if this
    does not actually mint a usable token."""
    r = client.post("/api/auth/login",
                    data={"username": "admin_test", "password": "Admin123!"})
    assert r.status_code == 200, r.text
    refresh = r.json()["refresh_token"]

    r2 = client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert r2.status_code == 200, r2.text
    fresh = r2.json()["access_token"]

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {fresh}"})
    assert me.status_code == 200, "the refreshed token does not authenticate"


def test_a_junk_refresh_token_is_refused():
    """The UI falls back to the login redirect on a failed refresh, so this must
    fail rather than mint something."""
    from suni import auth
    assert not auth.verify_refresh_token("not-a-token")
    assert not auth.verify_refresh_token("")
