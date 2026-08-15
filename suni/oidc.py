"""
OIDC / SSO for SUNI — Authorization Code Flow with PKCE.

Grafts single-sign-on onto SUNI's existing JWT + RBAC (auth.py) without changing
the local password path. Works with any OIDC provider that publishes a
`.well-known/openid-configuration` document (Google, Entra ID, Okta, Keycloak…).

Configuration is via environment variables ONLY (never the committed config
JSON), so the client secret never lands in the repo:

    SUNI_OIDC_ISSUER           https://accounts.google.com   (required)
    SUNI_OIDC_CLIENT_ID        ...                            (required)
    SUNI_OIDC_CLIENT_SECRET    ...                            (required, server-only)
    SUNI_OIDC_SCOPES           "openid email profile"         (default)
    SUNI_OIDC_PROVIDER_NAME    "SSO"                          (login button text)
    SUNI_OIDC_ROLE_CLAIM       e.g. "groups" or "roles"       (optional)
    SUNI_OIDC_ROLE_MAP         "grp-admins:admin,grp-ops:power-user"  (optional)
    SUNI_OIDC_DEFAULT_ROLE     "read-only"                    (unmapped users)
    SUNI_OIDC_PASSWORD_ENABLED "true"                         ("false" = SSO-only)
    SUNI_OIDC_REDIRECT_BASE    "https://suni.example.com"     (reverse-proxy origin)

OIDC stays inert until issuer+client_id+client_secret are all set (enabled()).

Security posture (each item unit-tested offline in tests, no live IdP needed):
  • ID token fully validated: JWKS signature (kid), iss, aud==client_id, exp/iat
    (with small leeway), and nonce compared to the value stored with the state.
  • State is single-use (atomic pop), TTL 5 min, carries nonce + PKCE verifier.
  • `next` redirect target validated to a local path (no open redirect).
  • First login never adopts an existing local username (dedupe); setup-guarded.
  • Client secret only used server→IdP; TLS verification always on.
"""
from __future__ import annotations
import base64
import hashlib
import logging
import os
import secrets
import threading
import time
from urllib.parse import urlencode

import httpx
from jose import jwt

from . import auth as _auth

log = logging.getLogger("suni.oidc")

_STATE_TTL   = 300      # seconds a pending authorization may sit before expiry
_HTTP_TIMEOUT = 10.0    # seconds for discovery / token / jwks calls
_LEEWAY      = 30       # seconds of clock skew tolerated on exp/iat


class OidcError(Exception):
    """User-facing OIDC failure (login refused)."""


# ── Configuration ─────────────────────────────────────────────────────────────

def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def config() -> dict:
    return {
        "issuer":        _env("SUNI_OIDC_ISSUER").rstrip("/"),
        "client_id":     _env("SUNI_OIDC_CLIENT_ID"),
        "client_secret": _env("SUNI_OIDC_CLIENT_SECRET"),
        "scopes":        _env("SUNI_OIDC_SCOPES", "openid email profile"),
        "provider_name": _env("SUNI_OIDC_PROVIDER_NAME", "SSO"),
        "role_claim":    _env("SUNI_OIDC_ROLE_CLAIM"),
        "role_map":      _env("SUNI_OIDC_ROLE_MAP"),
        "default_role":  _env("SUNI_OIDC_DEFAULT_ROLE", "read-only"),
        "password_enabled": _env("SUNI_OIDC_PASSWORD_ENABLED", "true").lower() != "false",
        "redirect_base": _env("SUNI_OIDC_REDIRECT_BASE").rstrip("/"),
    }


def enabled() -> bool:
    c = config()
    return bool(c["issuer"] and c["client_id"] and c["client_secret"])


def public_status() -> dict:
    """Non-secret fields the login page needs (safe to expose unauthenticated)."""
    c = config()
    on = enabled()
    return {
        "oidc_enabled":       on,
        "oidc_provider_name": c["provider_name"] if on else "",
        # In SSO-only mode we still allow password login while there are zero
        # users, so the first admin can be bootstrapped via the setup wizard.
        "password_enabled":   c["password_enabled"] or _auth.is_first_run(),
    }


# ── Discovery (cached) ────────────────────────────────────────────────────────

_disco_lock = threading.Lock()
_disco_cache: dict = {}


async def _discover() -> dict:
    c = config()
    iss = c["issuer"]
    if not iss:
        raise OidcError("OIDC issuer not configured.")
    with _disco_lock:
        if _disco_cache.get("issuer") == iss:
            return _disco_cache["doc"]
    url = f"{iss}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        doc = r.json()
    # The discovery document's own issuer must match what we configured — else a
    # tampered/misconfigured endpoint could point us at an attacker's token URL.
    if doc.get("issuer", "").rstrip("/") != iss:
        raise OidcError("OIDC discovery issuer mismatch.")
    for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not doc.get(k):
            raise OidcError(f"OIDC discovery missing {k}.")
    with _disco_lock:
        _disco_cache.clear()
        _disco_cache.update(issuer=iss, doc=doc)
    return doc


async def _fetch_jwks(jwks_uri: str) -> dict:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=True) as client:
        r = await client.get(jwks_uri)
        r.raise_for_status()
        return r.json()


# ── Pending-state store (single-worker in-memory; single-use) ─────────────────

_states_lock = threading.Lock()
_states: dict[str, dict] = {}


def _sweep_locked() -> None:
    now = time.time()
    for s in [k for k, v in _states.items() if now - v["created"] > _STATE_TTL]:
        _states.pop(s, None)


def _put_state(entry: dict) -> str:
    state = secrets.token_urlsafe(24)
    with _states_lock:
        _sweep_locked()
        _states[state] = entry
    return state


def _pop_state(state: str) -> dict | None:
    """Atomically remove and return the pending entry — a replayed state can't be
    reused, and an expired one is rejected."""
    with _states_lock:
        _sweep_locked()
        entry = _states.pop(state, None)
    if not entry:
        return None
    if time.time() - entry["created"] > _STATE_TTL:
        return None
    return entry


# ── Helpers ───────────────────────────────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def safe_next(next_url: str | None) -> str:
    """Only allow local same-site paths — blocks open-redirect via `next`."""
    if not next_url or not next_url.startswith("/"):
        return "/"
    if next_url.startswith("//") or "\\" in next_url or "://" in next_url:
        return "/"
    return next_url


def _redirect_uri(request_base: str) -> str:
    c = config()
    base = c["redirect_base"] or request_base.rstrip("/")
    return f"{base}/api/auth/oidc/callback"


def _map_role(claims: dict) -> str | None:
    """Map an IdP role/group claim to a SUNI role via SUNI_OIDC_ROLE_MAP. Accepts
    a claim that is either a single string or a list. Returns the HIGHEST-privilege
    mapped role among matches, or None if nothing matched."""
    c = config()
    claim_name, raw_map = c["role_claim"], c["role_map"]
    if not claim_name or not raw_map:
        return None
    mapping: dict[str, str] = {}
    for pair in raw_map.split(","):
        if ":" in pair:
            k, v = pair.split(":", 1)
            mapping[k.strip()] = v.strip()
    val = claims.get(claim_name)
    values = val if isinstance(val, list) else [val]
    matched = [mapping[str(v)] for v in values if str(v) in mapping]
    if not matched:
        return None
    # highest privilege wins (auth.ROLES is ascending)
    return max(matched, key=lambda r: _auth.ROLES.index(r) if r in _auth.ROLES else -1)


def _derive_username(claims: dict) -> str:
    base = (claims.get("preferred_username")
            or (claims.get("email") or "").split("@")[0]
            or claims.get("name") or "user")
    base = "".join(ch for ch in str(base).lower() if ch.isalnum() or ch in "._-") or "user"
    return _auth.dedupe_username(base)


# ── ID-token validation (pure; unit-tested offline) ───────────────────────────

def validate_id_token(id_token: str, jwks: dict, issuer: str, client_id: str,
                      expected_nonce: str, leeway: int = _LEEWAY) -> dict:
    """
    Fully validate an OIDC ID token. Raises OidcError on ANY failure. Checks:
    signature (JWKS key by kid), iss, aud==client_id, exp/iat (with leeway), and
    nonce == the value we stored with the state. Returns the validated claims.
    """
    try:
        header = jwt.get_unverified_header(id_token)
    except Exception as exc:
        raise OidcError(f"Malformed ID token header: {exc}")
    kid = header.get("kid")
    keys = jwks.get("keys", [])
    key = next((k for k in keys if k.get("kid") == kid), None)
    # Some providers omit kid when the JWKS has a single key.
    if key is None and len(keys) == 1:
        key = keys[0]
    if key is None:
        raise OidcError("No matching JWKS key for ID token.")
    try:
        claims = jwt.decode(
            id_token, key,
            algorithms=["RS256", "ES256", "RS384", "ES384", "RS512", "ES512"],
            audience=client_id,          # WITHOUT this, jose does NOT verify aud
            issuer=issuer,
            options={
                "require_aud": True, "require_exp": True, "require_iat": True,
                "verify_aud": True, "verify_iss": True, "verify_exp": True,
                "leeway": leeway,
            },
        )
    except Exception as exc:
        raise OidcError(f"ID token validation failed: {exc}")
    # nonce is not a standard jose-verified claim — compare it ourselves.
    if not expected_nonce or claims.get("nonce") != expected_nonce:
        raise OidcError("ID token nonce mismatch (possible replay).")
    return claims


# ── Public flow ───────────────────────────────────────────────────────────────

async def build_authorize_url(request_base: str, next_url: str | None) -> str:
    c = config()
    doc = await _discover()
    verifier  = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    nonce     = secrets.token_urlsafe(24)
    redirect_uri = _redirect_uri(request_base)
    state = _put_state({
        "nonce": nonce, "verifier": verifier, "next": safe_next(next_url),
        "redirect_uri": redirect_uri, "created": time.time(),
    })
    params = {
        "response_type": "code",
        "client_id":     c["client_id"],
        "redirect_uri":  redirect_uri,
        "scope":         c["scopes"],
        "state":         state,
        "nonce":         nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{doc['authorization_endpoint']}?{urlencode(params)}"


async def handle_callback(code: str, state: str) -> tuple[dict, str]:
    """
    Complete the flow: validate state, exchange the code (with PKCE verifier),
    validate the ID token, and JIT-provision / role-sync the local user.
    Returns (user_dict, next_url). Raises OidcError on failure.
    """
    if not code or not state:
        raise OidcError("Missing code or state.")
    entry = _pop_state(state)          # single-use + TTL enforced
    if not entry:
        raise OidcError("Invalid or expired login state. Please try again.")

    c = config()
    doc = await _discover()
    data = {
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  entry["redirect_uri"],
        "client_id":     c["client_id"],
        "client_secret": c["client_secret"],   # server→IdP only
        "code_verifier": entry["verifier"],
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, verify=True) as client:
        r = await client.post(doc["token_endpoint"], data=data,
                              headers={"Accept": "application/json"})
    if r.status_code != 200:
        log.warning("[OIDC] token endpoint returned %s", r.status_code)
        raise OidcError("Token exchange failed.")
    tok = r.json()
    id_token = tok.get("id_token")
    if not id_token:
        raise OidcError("No ID token in provider response.")

    jwks = await _fetch_jwks(doc["jwks_uri"])
    claims = validate_id_token(id_token, jwks, c["issuer"], c["client_id"], entry["nonce"])
    user = _provision(c["issuer"], claims)
    return user, safe_next(entry["next"])


def _provision(issuer: str, claims: dict) -> dict:
    sub = claims.get("sub")
    if not sub:
        raise OidcError("ID token missing subject.")
    role_from_claim = _map_role(claims)

    existing = _auth.get_user_by_oidc(issuer, sub)
    if existing:
        if not existing.get("active", 1):
            raise OidcError("Account is disabled.")
        if role_from_claim:
            _auth.sync_oidc_role(existing["id"], role_from_claim)
        _auth._record_login(existing["id"])
        return _auth.get_user(existing["id"])

    # First login for this identity. Never let the first-ever user be created via
    # SSO — the initial admin must be a local account from the setup wizard, so an
    # external identity can never bootstrap itself into admin.
    if _auth.is_first_run():
        raise OidcError("Complete the first-run admin setup before using SSO.")

    username = _derive_username(claims)     # deduped — never adopts a local account
    role = role_from_claim or config()["default_role"]
    user = _auth.create_oidc_user(issuer, sub, username, role)
    log.info("[OIDC] provisioned SSO user %s (role=%s) for %s/%s",
             username, user["role"], issuer, sub[:8])
    return user
