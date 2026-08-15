"""
Lightweight symmetric encryption for storing per-user secrets (e.g. API keys).

Key derivation: HKDF-like SHA-256 over the server's existing jwt_secret.txt so no
extra key management is needed.  If jwt_secret.txt doesn't exist yet (first-run race)
we silently fall back to identity (no encryption) and log a warning.
"""
from __future__ import annotations
import base64
import hashlib
import logging
from pathlib import Path

_log = logging.getLogger("suni.crypto_utils")
_JWT_SECRET_PATH = Path("memory/jwt_secret.txt")
_PURPOSE = b":suni-settings-v1"


class EncryptionUnavailable(RuntimeError):
    """Raised when a secret cannot be encrypted (no key) — callers must fail closed."""


def _fernet():
    from cryptography.fernet import Fernet
    try:
        raw = _JWT_SECRET_PATH.read_bytes().strip()
    except FileNotFoundError:
        _log.warning("jwt_secret.txt not found — API key encryption unavailable")
        return None
    derived = hashlib.sha256(raw + _PURPOSE).digest()
    key = base64.urlsafe_b64encode(derived)
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    """
    Return base64url-encoded ciphertext, or '' for empty input.

    Fails closed: if no encryption key is available, raises EncryptionUnavailable
    rather than silently persisting the secret in plaintext. Callers must catch
    it and refuse to store the secret.
    """
    if not plaintext:
        return ""
    f = _fernet()
    if f is None:
        raise EncryptionUnavailable(
            "encryption key unavailable (memory/jwt_secret.txt missing) — "
            "refusing to store secret as plaintext"
        )
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Return plaintext, or '' on any error (no key, bad key, tampered, empty)."""
    if not ciphertext:
        return ""
    f = _fernet()
    if f is None:
        _log.warning("encryption key unavailable — cannot decrypt stored secret")
        return ""
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except Exception:
        return ""
