"""Notice when SUNI tells someone she cannot do something.

This is how the missing meeting tools would have been found: a user asked for
a transcript, she answered "I can't join the call", and the only record of it
was the conversation. Here the start of the request and of the answer are
kept, so an admin can see what people keep asking for that she does not do.

A list for a person to read, never a fault to fix: a request is not an error,
and acting on one would let anyone who can chat with SUNI write her roadmap.
Only the opening of the reply is read — a refusal comes first — and a reply
that merely quotes someone else's "I can't" further down is not one.
"""
from __future__ import annotations

import logging
import re

from . import store

log = logging.getLogger("suni.upkeep")

_CANT = re.compile(
    r"\b(?:"
    r"I\s+(?:can(?:no|')t|am\s+(?:unable|not\s+able)\s+to|do(?:n't|\s+not)\s+have\s+"
    r"(?:a\s+way|the\s+ability|access|a\s+tool))"
    r"|I'm\s+(?:unable|not\s+able)\s+to"
    r"|não\s+(?:consigo|posso|tenho\s+(?:como|acesso|forma|maneira|nenhuma\s+ferramenta))"
    r"|não\s+me\s+é\s+possível|não\s+sou\s+capaz"
    r")",
    re.IGNORECASE)

READ_CHARS = 300
REQUEST_CHARS = 120
REPLY_CHARS = 200


def is_refusal(reply: str) -> bool:
    return bool(_CANT.search((reply or "")[:READ_CHARS]))


def _key(request: str) -> str:
    s = re.sub(r"[^\w\s]", " ", request.lower())
    return " ".join(s.split())[:60]


def note_reply(user_id: str, request: str, reply: str) -> None:
    """Called after every turn. Cheap when there is nothing to note, and never raises."""
    try:
        from .. import config as _cfg
        if not _cfg.get("upkeep_enabled") or not isinstance(reply, str) or not is_refusal(reply):
            return
        from ..diagnostics import _scrub
        store.record_gap(user_id or "", _key(request or ""),
                         _scrub((request or "").strip())[:REQUEST_CHARS],
                         _scrub(reply.strip())[:REPLY_CHARS])
    except Exception as exc:      # noqa: BLE001 — a turn must never fail on this
        log.debug("[UPKEEP] gap not noted: %s", exc)
