"""
Auto-configuration: detects user preferences from conversation and applies them
to user_settings automatically.

Scope: ONLY writes to user_settings fields. Can NEVER touch suni_config (global).
Always reversible: SUNI announces changes; user can say "undo that".
"""
from __future__ import annotations
import re
from . import user_settings as _us

# ── Language detection ────────────────────────────────────────────────────────

_LANG_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b(respond|answer|reply|write|speak|talk)\s+(to\s+me\s+)?(in|using)\s+(?:european\s+)?portuguese\b', re.I), "pt-PT"),
    (re.compile(r'\b(respond|answer|reply|write|speak)\s+(to\s+me\s+)?(in|using)\s+(?:brazilian\s+)?portuguese\b', re.I), "pt-BR"),
    (re.compile(r'\bfala\s+(comigo\s+em\s+)?portugu[eê]s\b', re.I), "pt-PT"),
    (re.compile(r'\b(responde|responda|fala)\s+(em\s+)?portugu[eê]s\b', re.I), "pt-PT"),
    (re.compile(r'\b(respond|answer|reply)\s+(to\s+me\s+)?(in|using)\s+english\b', re.I), "en-GB"),
    (re.compile(r'\b(respond|answer|reply)\s+(to\s+me\s+)?(in|using)\s+spanish\b', re.I), "es-ES"),
    (re.compile(r'\b(respond|answer|reply)\s+(to\s+me\s+)?(in|using)\s+(french|français)\b', re.I), "fr-FR"),
    (re.compile(r'\b(respond|answer|reply)\s+(to\s+me\s+)?(in|using)\s+german\b', re.I), "de-DE"),
]

# ── MCP preference detection ──────────────────────────────────────────────────

_MCP_DISABLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bdon'?t\s+(need|use|want)\s+(the\s+)?browser\s+tools?\b", re.I), "playwright"),
    (re.compile(r"\bdisable\s+(the\s+)?playwright\b", re.I), "playwright"),
    (re.compile(r"\bno\s+browser\s+(access|tools?|automation)\b", re.I), "playwright"),
]


def detect_and_apply(user_id: str, user_msg: str, assistant_response: str) -> list[str]:
    """
    Analyse user_msg (and optionally assistant_response) for preference signals.
    Apply detected changes to user_settings. Returns list of human-readable change
    descriptions (empty if no changes).
    """
    changes: list[str] = []
    settings = _us.get(user_id)

    # ── Language preference ───────────────────────────────────────────────────
    for pattern, lang_code in _LANG_PATTERNS:
        if pattern.search(user_msg):
            current = settings.get("response_language", "")
            if current != lang_code:
                _us.save(user_id, {"response_language": lang_code})
                lang_name = {
                    "pt-PT": "European Portuguese",
                    "pt-BR": "Brazilian Portuguese",
                    "en-GB": "English",
                    "es-ES": "Spanish",
                    "fr-FR": "French",
                    "de-DE": "German",
                }.get(lang_code, lang_code)
                changes.append(f"response language: {lang_name}")
            break

    # ── MCP server disabling ──────────────────────────────────────────────────
    for pattern, server_name in _MCP_DISABLE_PATTERNS:
        if pattern.search(user_msg):
            current_mcp = settings.get("allowed_mcp_servers")
            if current_mcp is None:
                # Was inheriting from role; create explicit list without this server
                # We don't know the full role list here, so just record the exclusion
                # via preference memory — full disabling requires knowing the role's list
                pass  # Too complex to handle without role context; skip
            elif isinstance(current_mcp, list) and server_name in current_mcp:
                new_list = [s for s in current_mcp if s != server_name]
                _us.save(user_id, {"allowed_mcp_servers": new_list})
                changes.append(f"disabled MCP server '{server_name}'")

    return changes
