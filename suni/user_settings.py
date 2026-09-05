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
import re
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
# Inside the install, not in anyone's personal folder. Relative, so it
# follows the working directory the way every other SUNI store does.
_DEFAULT_OUTPUT_ROOT = Path("files") / "output"


def output_root() -> Path:
    """The one directory every generated file lives under.

    Defaults to `files/output` INSIDE the install rather than the Desktop of
    whichever account happens to run SUNI. The Desktop default was written when
    SUNI was a single-user tool on one person's machine; the moment other people
    reach it over the network, "generated files land in the operator's personal
    folder" stops being convenient and starts being a leak.
    """
    from . import config as _cfg
    raw = (_cfg.get("global_output_dir") or "").strip()
    return Path(raw) if raw else _DEFAULT_OUTPUT_ROOT


def user_folder_name(user_id: str) -> str:
    """A readable, unique, filesystem-safe folder name for one user.

    The username makes it possible to find your own files by looking; the id
    suffix makes collisions impossible, because two different usernames can
    sanitise to the same string and quietly share a folder — which is exactly
    the isolation this is here to provide.
    """
    name = ""
    try:
        from . import auth as _auth
        u = _auth.get_user(user_id)
        if u:
            name = str(u.get("username") or "")
    except Exception:                      # noqa: BLE001 — never block a write
        pass
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._-")[:40]
    short = re.sub(r"[^A-Za-z0-9]", "", str(user_id))[:8] or "anon"
    return f"{safe}-{short}" if safe else short


def resolve_output_dir(user_id: str = "") -> str:
    """Where this user's generated files go.

    Every user gets their OWN subdirectory of the root. Before, everyone shared
    one directory, so on a networked instance each person's documents sat beside
    everybody else's and the file-serve endpoint would hand any of them to any
    account that asked.

    A user may still set their own `output_dir`, and it is still only honoured
    when it resolves inside the root — otherwise it would be a way to point the
    serve endpoint at any path on the machine.
    """
    root = output_root()
    try:
        root_p = root.resolve()
        root_p.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _DESKTOP_FALLBACK           # unwritable root: better than nothing

    if not user_id:
        # Scheduled runs and channel gateways with no signed-in user. Kept out
        # of every real user's folder rather than dropped in the root, where it
        # would be served to whoever asked.
        shared = root_p / "_system"
        try:
            shared.mkdir(parents=True, exist_ok=True)
            return str(shared)
        except OSError:
            return str(root_p)

    user_raw = get(user_id).get("output_dir", "").strip()
    if user_raw:
        try:
            user_p = Path(user_raw).resolve()
            user_p.relative_to(root_p)     # ValueError if it escapes the root
            user_p.mkdir(parents=True, exist_ok=True)
            return str(user_p)
        except (ValueError, OSError):
            pass                           # outside the root, or unusable

    own = root_p / user_folder_name(user_id)
    try:
        own.mkdir(parents=True, exist_ok=True)
        return str(own)
    except OSError:
        return str(root_p)
