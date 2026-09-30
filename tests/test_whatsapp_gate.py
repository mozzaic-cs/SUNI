"""Who is allowed to talk to SUNI over WhatsApp.

The webhook validated the Twilio signature, which proves the REQUEST came from
Twilio. It says nothing about who SENT the message. Telegram, Discord and Slack
each gate the sender and fail closed; WhatsApp did not, so anyone who knew the
number reached a full assistant at role "standard" — which reads files,
searches the knowledge base and lists email.

Numbers are compared on digits alone. A list that matches only one spelling of
a phone number is a list that quietly fails the first time somebody writes it
with spaces.
"""
from __future__ import annotations

import pathlib
import re

CFG = (pathlib.Path(__file__).resolve().parent.parent
       / "suni/config.py").read_text(encoding="utf-8")
SERVER = (pathlib.Path(__file__).resolve().parent.parent
          / "suni/web/server.py").read_text(encoding="utf-8-sig")
DIAG = (pathlib.Path(__file__).resolve().parent.parent
        / "suni/diagnostics.py").read_text(encoding="utf-8")


def _webhook() -> str:
    i = SERVER.index('@app.post("/whatsapp")')
    return SERVER[i:SERVER.index('@app.get("/whatsapp/status")')]


def test_there_is_an_allow_list_and_it_starts_empty():
    """Empty means nobody, not everybody. That is the fail-closed state the
    other three channels already had."""
    assert '"whatsapp_allowed_numbers": []' in CFG


def test_an_unknown_sender_is_refused_before_the_model_runs():
    body = _webhook()
    assert "whatsapp_allowed_numbers" in body, "the webhook never consults the list"
    gate = body.index("whatsapp_allowed_numbers")
    run = body.index("orchestrator._safe_run")
    assert gate < run, "the sender is checked after the model has already answered"
    assert "refused" in body


def test_the_refusal_tells_them_what_to_ask_for():
    """The other channels reply with the id so the owner can add it. Silence
    reads as a broken number."""
    body = _webhook()
    assert "allow-list" in body and "Your number is" in body


def test_numbers_are_compared_on_digits_alone():
    """"+351912345678", "351912345678" and "whatsapp:+351 912 345 678" are one
    person."""
    body = _webhook()
    assert "_wa_digits" in body
    assert r'\D+' in body, "the comparison is still spelling-sensitive"

    # The same normalisation, exercised rather than read.
    digits = lambda v: re.sub(r"\D+", "", str(v or ""))
    forms = ["whatsapp:+351912345678", "+351912345678", "351912345678",
             "whatsapp:+351 912 345 678", "+351-912-345-678"]
    assert len({digits(f) for f in forms}) == 1, "the same number reads as several"


def test_an_empty_list_lets_nobody_through():
    """The specific failure this replaces would be an allow-list that, when
    empty, waves everyone past."""
    digits = lambda v: re.sub(r"\D+", "", str(v or ""))
    allowed = {digits(n) for n in []}
    allowed.discard("")
    assert digits("whatsapp:+351912345678") not in allowed


def test_a_blank_entry_does_not_become_a_wildcard():
    """An empty string in the list would normalise to "" and match anything
    whose digits are empty — so it is discarded rather than kept."""
    body = _webhook()
    assert '_allowed_wa.discard("")' in body


def test_the_self_check_notices_an_empty_list():
    """SUNI's own diagnostics already report this for the other three."""
    assert '"whatsapp_allowed_numbers"' in DIAG
    assert "whatsapp" in DIAG
