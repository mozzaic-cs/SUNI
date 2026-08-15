"""
Per-user preference store.  One JSON file per user: memory/users/{user_id}/settings.json

Keys (all optional):
  stt_language          BCP-47 code for speech recognition, e.g. "pt-PT"
  response_language     BCP-47 code for AI response language, e.g. "pt-PT"
  tts_voice             edge-tts voice name, e.g. "pt-PT-RaquelNeural"
  allowed_mcp_servers   list of MCP server prefixes this user may access,
                        or null/absent to inherit from role. E.g. ["playwright", "filesystem"]
  anthropic_api_key     Fernet-encrypted Anthropic API key for this user's Claude Code account.
                        Empty string = use the globally configured ANTHROPIC_API_KEY env var.
  output_dir            Absolute path for generated files. Must be inside global_output_dir.
                        Empty = use global_output_dir (or Desktop if that is also empty).
  smtp_host/_port/_user/_pass, notify_to, imap_host/_port
                        This user's own mail account, so each person sends as
                        themselves rather than through one shared mailbox.
                        smtp_pass is Fernet-encrypted at rest. Any field left
                        empty falls back to the admin-configured account, and
                        then to the environment — see resolve_email().
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "stt_language":        "",
    "response_language":   "",
    "tts_voice":           "",
    "allowed_mcp_servers": None,   # None = inherit from role
    "anthropic_api_key":   "",     # encrypted; empty = use global env var
    "output_dir":          "",     # per-user output directory (must be under global_output_dir)
    # Per-user mail account — each empty field falls back to the global config.
    "smtp_host":           "",
    "smtp_port":           0,      # 0 = not set (fall back)
    "smtp_user":           "",
    "smtp_pass":           "",     # encrypted; empty = fall back
    "notify_to":           "",
    "imap_host":           "",
    "imap_port":           0,      # 0 = not set (fall back)
}

# Per-user keys held encrypted at rest.
_ENCRYPTED_KEYS = ("anthropic_api_key", "smtp_pass")


def _path(user_id: str) -> Path:
    p = Path(f"memory/users/{user_id}/settings.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get(user_id: str) -> dict:
    try:
        return {**DEFAULTS, **json.loads(_path(user_id).read_text(encoding="utf-8"))}
    except Exception:
        return dict(DEFAULTS)


def save(user_id: str, data: dict) -> dict:
    allowed = set(DEFAULTS.keys())
    current = get(user_id)
    patch   = {k: v for k, v in data.items() if k in allowed}
    # allowed_mcp_servers: accept list or None; reject other types
    if "allowed_mcp_servers" in patch:
        v = patch["allowed_mcp_servers"]
        if v is not None and not isinstance(v, list):
            patch.pop("allowed_mcp_servers")
    # Secrets: encrypt any plaintext value being saved
    for _ek in _ENCRYPTED_KEYS:
        if _ek in patch and patch[_ek]:
            raw = patch[_ek]
            # Only encrypt if it looks like a raw value (not already Fernet ciphertext)
            if isinstance(raw, str) and not raw.startswith("gAAAA"):
                from .crypto_utils import encrypt, EncryptionUnavailable
                try:
                    patch[_ek] = encrypt(raw)
                except EncryptionUnavailable:
                    # Fail closed: never persist the secret in plaintext. Drop it
                    # from the patch (existing stored value is left untouched).
                    import logging
                    logging.getLogger("suni.user_settings").error(
                        "cannot encrypt %s (no encryption key) — not storing it", _ek)
                    patch.pop(_ek, None)
    merged  = {**current, **patch}
    _path(user_id).write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    return merged


def get_decrypted_api_key(user_id: str) -> str:
    """Return the plaintext Anthropic API key for this user, or '' if not set."""
    key = get(user_id).get("anthropic_api_key", "")
    if not key:
        return ""
    from .crypto_utils import decrypt
    return decrypt(key)


def resolve_email(user_id: str = "") -> dict | None:
    """Return the effective mail account, or None if none is configured.

    Priority per field: this user's setting → the admin-configured account →
    the environment. Resolution is per FIELD, not per level, so a user who sets
    only smtp_user/smtp_pass still inherits the admin's host and port.

    Background jobs (the inbox watcher, article notifications) call this with no
    user_id and therefore always use the admin/environment account — they run
    outside any request and have no user to attribute mail to.
    """
    import os
    from . import config as _cfg

    us = get(user_id) if user_id else {}

    def pick(key: str, env: str, default: str = "") -> str:
        v = str(us.get(key, "") or "").strip()
        if v:
            return v
        v = str(_cfg.get(key, "") or "").strip()
        if v:
            return v
        return os.environ.get(env, default)

    def pick_port(key: str, env: str, default: int) -> int:
        for raw in (us.get(key), _cfg.get(key, 0), os.environ.get(env)):
            try:
                n = int(raw)          # 0 / "" / None all mean "not set"
                if n > 0:
                    return n
            except (TypeError, ValueError):
                continue
        return default

    host = pick("smtp_host", "SUNI_SMTP_HOST")
    user = pick("smtp_user", "SUNI_SMTP_USER")

    # The password is the one field that must come from the same level as the
    # username — inheriting a password that belongs to a different account would
    # just fail to authenticate, confusingly.
    if str(us.get("smtp_user", "") or "").strip():
        pw = str(us.get("smtp_pass", "") or "")
        if pw:
            from .crypto_utils import decrypt
            pw = decrypt(pw)
    else:
        pw = pick("smtp_pass", "SUNI_SMTP_PASS")

    if not (host and user and pw):
        return None

    return {
        "host":     host,
        "port":     pick_port("smtp_port", "SUNI_SMTP_PORT", 587),
        "user":     user,
        "password": pw,
        "to":       pick("notify_to", "SUNI_NOTIFY_TO") or user,
        "imap_host": pick("imap_host", "SUNI_IMAP_HOST"),
        "imap_port": pick_port("imap_port", "SUNI_IMAP_PORT", 993),
    }


_DESKTOP_FALLBACK = str(Path.home() / "Desktop")


def resolve_output_dir(user_id: str = "") -> str:
    """Return the effective output directory for generated files.

    Priority: user's output_dir (validated under global_output_dir)
              → global_output_dir
              → Desktop fallback.

    Security: a user's output_dir is only honoured when it is a subdirectory
    of global_output_dir.  This prevents users from reading arbitrary paths via
    the file-serve endpoint by setting output_dir to an arbitrary location.
    """
    from . import config as _cfg
    global_raw = (_cfg.get("global_output_dir") or "").strip()

    if user_id:
        user_raw = get(user_id).get("output_dir", "").strip()
        if user_raw and global_raw:
            try:
                user_p   = Path(user_raw).resolve()
                global_p = Path(global_raw).resolve()
                user_p.relative_to(global_p)   # raises ValueError if not under global
                user_p.mkdir(parents=True, exist_ok=True)
                return str(user_p)
            except (ValueError, OSError):
                pass  # fall through to global

    if global_raw:
        try:
            global_p = Path(global_raw).resolve()
            global_p.mkdir(parents=True, exist_ok=True)
            return str(global_p)
        except OSError:
            pass

    return _DESKTOP_FALLBACK
