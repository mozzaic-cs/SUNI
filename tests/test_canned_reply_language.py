"""
Replies SUNI writes in Python must honour the user's language.

Reported from live use: a pt-PT account asked, in Portuguese, for a PDF about
Coimbra to be emailed. SUNI answered in English — "I could not find any compiled
content to put into a PDF. Please compile the information first" — and did no
research at all.

Two separate defects behind one symptom.

The language one is here: `_lang_instruction` only reaches the MODEL, so any
sentence composed in Python is English whatever the user has configured. Five
such strings existed, one of them an outgoing EMAIL BODY.

The other defect is in test_pdf_route_fallback.py: the direct-pdf path required
content that did not exist and dead-ended instead of handing back.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from suni.core import orchestrator as orch

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "suni" / "core" / "orchestrator.py").read_text(encoding="utf-8")

KEYS = ["no_email_address", "plan_cancelled", "image_failed", "attached"]


@pytest.mark.parametrize("key", KEYS)
def test_every_canned_reply_has_portuguese(key):
    """pt-PT is this deployment's configured language; it is the one that must
    not fall through to English."""
    assert orch._say(key, "pt-PT"), f"{key} has no pt-PT text"
    assert orch._say(key, "pt-PT") != orch._say(key, "en-GB"), \
        f"{key} returns the English string for pt-PT"


@pytest.mark.parametrize("key", KEYS)
def test_english_is_the_fallback_not_a_missing_key(key):
    """An unsupported locale should degrade to English, never to ''."""
    assert orch._say(key, "ja-JP") == orch._say(key, "en-GB")
    assert orch._say(key, "") != ""


def test_a_bare_language_tag_resolves():
    """'pt' should find pt-PT rather than dropping to English."""
    assert orch._say("attached", "pt") != orch._say("attached", "en-GB")


def test_an_unknown_key_returns_empty_rather_than_raising():
    assert orch._say("no_such_message", "pt-PT") == ""


def test_no_canned_reply_is_still_hardcoded_at_its_call_site():
    """The regression to catch: someone adds a new Python-composed reply and
    writes the sentence inline, where no language can reach it."""
    stragglers = []
    for m in re.finditer(r'content="([A-Z][^"]{30,})"', SRC):
        stragglers.append(m.group(1)[:60])
    assert not stragglers, (
        f"user-facing replies written inline instead of via _say(): {stragglers}")


def test_the_outgoing_email_body_is_translated():
    """This one leaves the machine — an English body in a Portuguese thread is
    the most visible version of the bug."""
    i = SRC.index("body = _say(")
    assert '"attached"' in SRC[i:i + 80]
    assert orch._say("attached", "pt-PT").startswith("Segue")


def test_the_language_table_covers_the_configured_languages():
    """Every language SUNI offers an instruction for should have canned text,
    or that language gets English replies for these paths."""
    offered = set(orch._LANG_MAP)
    for key in KEYS:
        table = orch._CANNED[key]
        missing = {l for l in offered
                   if l not in table and l.split("-")[0] not in table}
        # French and German are knowingly absent and fall back to English.
        # Compared as a set: the earlier list form depended on set iteration
        # order and failed for no reason anyone could act on.
        assert missing <= {"fr-FR", "de-DE"}, \
            f"{key} is missing unexpected languages: {sorted(missing)}"
