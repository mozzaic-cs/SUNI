"""
SUNI user authentication — SQLite user store, bcrypt passwords, JWT tokens.

Database: memory/users.db
JWT secret: memory/jwt_secret.txt  (generated once, persistent)

Roles (ascending privilege):
  read-only   — search/read tools only, no write operations
  standard    — full personal tool use, no admin
  power-user  — all tools including shell (with denylist)
  admin       — all tools + user management + config changes
"""
from __future__ import annotations
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import bcrypt as _bcrypt
from jose import JWTError, jwt

_DB       = Path("memory/users.db")
_KEY_FILE = Path("memory/jwt_secret.txt")
_ALG      = "HS256"
_ACCESS_EXPIRE_H  = 8
_REFRESH_EXPIRE_D = 30

ROLES = ["read-only", "standard", "power-user", "admin"]


def _hash_pw(password: str) -> str:
    return _bcrypt.hashpw(password.encode()[:72], _bcrypt.gensalt(rounds=12)).decode()


def _verify_pw(password: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except Exception:
        return False


# ── JWT secret ────────────────────────────────────────────────────────────────

def _jwt_secret() -> str:
    _KEY_FILE.parent.mkdir(exist_ok=True)
    if _KEY_FILE.exists():
        s = _KEY_FILE.read_text().strip()
        if len(s) >= 32:
            return s
    s = secrets.token_hex(32)
    _KEY_FILE.write_text(s)
    return s

_SECRET = _jwt_secret()


# ── Database init ─────────────────────────────────────────────────────────────

# Sentinel password hash for SSO-provisioned users — bcrypt can never produce a
# hash starting with "!", so no password will ever verify against it. This is a
# BACKSTOP; verify_password_login() also rejects auth_source='oidc' explicitly.
OIDC_SENTINEL = "!oidc"


def init_db() -> None:
    _DB.parent.mkdir(exist_ok=True)
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          TEXT PRIMARY KEY,
                username    TEXT UNIQUE NOT NULL,
                password_h  TEXT NOT NULL,
                role        TEXT NOT NULL DEFAULT 'standard',
                active      INTEGER NOT NULL DEFAULT 1,
                api_token_h TEXT,
                created_at  TEXT NOT NULL,
                last_login  TEXT
            )""")
        # auth_source: 'local' (password) | 'oidc' (SSO). Role-sync only ever
        # touches oidc users, so a manually-set local role is never overwritten.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        if "auth_source" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN auth_source TEXT NOT NULL DEFAULT 'local'")
        # Maps an external identity (issuer + subject) to a local user. Returning
        # logins match ONLY on this link — never by username — so an IdP can never
        # be handed a pre-existing local account.
        c.execute("""
            CREATE TABLE IF NOT EXISTS oidc_identities (
                issuer     TEXT NOT NULL,
                sub        TEXT NOT NULL,
                user_id    TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (issuer, sub)
            )""")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB))
    c.row_factory = sqlite3.Row
    return c


# ── User CRUD ─────────────────────────────────────────────────────────────────

def is_first_run() -> bool:
    try:
        with _conn() as c:
            return c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    except Exception:
        return True


def create_user(username: str, password: str, role: str = "standard") -> dict:
    if role not in ROLES:
        raise ValueError(f"Invalid role: {role}")
    uid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO users (id, username, password_h, role, active, created_at) VALUES (?,?,?,?,1,?)",
            (uid, username.strip().lower(), _hash_pw(password), role, now),
        )
    return get_user(uid)


def get_user(user_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE username=?",
                        (username.strip().lower(),)).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, username, role, active, created_at, last_login FROM users ORDER BY username"
        ).fetchall()
    return [dict(r) for r in rows]


def update_user(user_id: str, **kwargs) -> dict | None:
    allowed = {"role", "active", "username"}
    fields  = {k: v for k, v in kwargs.items() if k in allowed}
    if "role" in fields and fields["role"] not in ROLES:
        raise ValueError(f"Invalid role: {fields['role']}")
    if not fields:
        return get_user(user_id)
    sets = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE users SET {sets} WHERE id=?", (*fields.values(), user_id))
    return get_user(user_id)


def set_password(user_id: str, new_password: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET password_h=? WHERE id=?",
                  (_hash_pw(new_password), user_id))


def delete_user(user_id: str) -> bool:
    with _conn() as c:
        n = c.execute("DELETE FROM users WHERE id=?", (user_id,)).rowcount
    return n > 0


# ── API tokens (programmatic access) ─────────────────────────────────────────

def generate_api_token(user_id: str) -> str:
    """Generate a persistent API token for a user. Returns the raw token (shown once)."""
    raw = secrets.token_urlsafe(32)
    with _conn() as c:
        c.execute("UPDATE users SET api_token_h=? WHERE id=?",
                  (_hash_pw(raw), user_id))
    return raw


def revoke_api_token(user_id: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET api_token_h=NULL WHERE id=?", (user_id,))


def verify_api_token(raw_token: str) -> dict | None:
    """Check raw token against all users' stored hashes. O(n) but n is small."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM users WHERE active=1 AND api_token_h IS NOT NULL"
        ).fetchall()
    for row in rows:
        if _verify_pw(raw_token, row["api_token_h"]):
            _record_login(row["id"])
            return dict(row)
    return None


# ── Authentication ────────────────────────────────────────────────────────────

def verify_password_login(username: str, password: str) -> dict | None:
    user = get_user_by_username(username)
    if not user or not user["active"]:
        return None
    # SSO-provisioned users have no usable password — reject explicitly rather
    # than relying on the sentinel hash failing bcrypt (primary gate, not backstop).
    if user.get("auth_source") == "oidc" or str(user["password_h"]).startswith("!"):
        return None
    if not _verify_pw(password, user["password_h"]):
        return None
    _record_login(user["id"])
    return user


# ── OIDC / SSO identities ─────────────────────────────────────────────────────

def get_user_by_oidc(issuer: str, sub: str) -> dict | None:
    """Return the local user linked to this external identity, or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT u.* FROM users u JOIN oidc_identities o ON o.user_id = u.id "
            "WHERE o.issuer=? AND o.sub=?", (issuer, sub)
        ).fetchone()
    return dict(row) if row else None


def _username_available(username: str) -> bool:
    return get_user_by_username(username) is None


def dedupe_username(base: str) -> str:
    """Return `base`, or base-2/base-3/... if taken. Never returns a taken name."""
    base = (base or "user").strip().lower() or "user"
    if _username_available(base):
        return base
    for i in range(2, 1000):
        cand = f"{base}-{i}"
        if _username_available(cand):
            return cand
    return f"{base}-{uuid.uuid4().hex[:6]}"


def create_oidc_user(issuer: str, sub: str, username: str, role: str = "read-only") -> dict:
    """
    JIT-provision an SSO user + link the external identity, atomically. The
    caller MUST have already deduped `username` against existing local users so
    this never adopts a pre-existing account. Password is the sentinel (unusable).
    """
    if role not in ROLES:
        role = "read-only"
    uid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO users (id, username, password_h, role, active, created_at, auth_source) "
            "VALUES (?,?,?,?,1,?,'oidc')",
            (uid, username.strip().lower(), OIDC_SENTINEL, role, now),
        )
        c.execute(
            "INSERT INTO oidc_identities (issuer, sub, user_id, created_at) VALUES (?,?,?,?)",
            (issuer, sub, uid, now),
        )
    _record_login(uid)
    return get_user(uid)


def sync_oidc_role(user_id: str, role: str) -> None:
    """Update an SSO user's role from IdP claims. No-op for local users, so a
    manually-assigned local role is never clobbered by claim mapping."""
    if role not in ROLES:
        return
    with _conn() as c:
        c.execute("UPDATE users SET role=? WHERE id=? AND auth_source='oidc'",
                  (role, user_id))


def _record_login(user_id: str) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET last_login=? WHERE id=?",
                  (datetime.now(timezone.utc).isoformat(), user_id))


# ── JWT ───────────────────────────────────────────────────────────────────────

def create_access_token(user: dict) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=_ACCESS_EXPIRE_H)
    payload = {
        "sub":      user["id"],
        "username": user["username"],
        "role":     user["role"],
        "exp":      exp,
        "type":     "access",
    }
    return jwt.encode(payload, _SECRET, algorithm=_ALG)


def create_refresh_token(user_id: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(days=_REFRESH_EXPIRE_D)
    return jwt.encode({"sub": user_id, "exp": exp, "type": "refresh"},
                      _SECRET, algorithm=_ALG)


def verify_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, _SECRET, algorithms=[_ALG])
        if payload.get("type") != "access":
            return None
        user_id = payload.get("sub")
        user = get_user(user_id)
        if not user or not user["active"]:
            return None
        # Attach fresh role from DB (in case it changed since token was issued)
        return {**user, "role": user["role"]}
    except JWTError:
        return None


def verify_refresh_token(token: str) -> str | None:
    """Returns user_id if refresh token is valid, else None."""
    try:
        payload = jwt.decode(token, _SECRET, algorithms=[_ALG])
        if payload.get("type") != "refresh":
            return None
        return payload.get("sub")
    except JWTError:
        return None
