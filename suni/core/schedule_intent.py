"""
Deterministic parsing of scheduling and delegation requests.

Why this exists: registering create_schedule and invoke_agent was not enough.
Across repeated live runs of the same prompt, the core tier called skills_list,
run_shell, db_query and send_email — never the right tool — with the tool
registered, a router hint injected, and the agent named in that hint. Selecting
one tool out of ~57 and filling its arguments correctly is the thing small
models are worst at, and it is the thing this request needs.

So the structured part is taken away from the model. A regex knows what "every
day at 8:00" means with total reliability; the model is left to do what it is
good at, which is writing the prompt that will run later.

The rule throughout: extract what is present, name what is missing, and never
invent. A schedule built on a guessed detail runs wrong every day, unattended,
with nobody watching it fail.
"""
from __future__ import annotations

import re
from typing import Any

_DOW = {"mon": 0, "monday": 0, "tue": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
        "thu": 3, "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
        "sun": 6, "sunday": 6}
_DOW_CANON = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_TIME = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_QUOTED = re.compile(r"[\"'“‘]([^\"'”’]{2,60})[\"'”’]")


def _clock(text: str) -> tuple[int, int] | None:
    """First wall-clock time in the text, as (hour, minute).

    Skips bare numbers that are not really times — "every 2 hours" must not be
    read as 02:00, which is exactly the sort of silent misreading this module is
    here to prevent.
    """
    for m in _TIME.finditer(text):
        h, mi, ap = int(m.group(1)), m.group(2), (m.group(3) or "").lower()
        tail = text[m.end():m.end() + 12].lower()
        if not mi and not ap and re.match(r"\s*(h|hr|hrs|hour|hours|m|min|mins|minutes)", tail):
            continue                                   # an interval, not a time
        if not mi and not ap and not re.search(r"\bat\s*$", text[:m.start()].lower()):
            continue                                   # a bare number, not a time
        mi = int(mi or 0)
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        if 0 <= h < 24 and 0 <= mi < 60:
            return h, mi
    return None


def parse_cadence_phrase(text: str) -> tuple[str | None, str | None]:
    """(cadence, missing) — a cadence string for suni.schedules, or what is absent.

    Returns (None, reason) when the intent is recurring but underspecified, so
    the caller can ask instead of assuming. Returns (None, None) when nothing
    recurring was asked for at all.
    """
    t = text.lower()

    m = re.search(r"\bevery\s+(\d+)\s*(m|min|mins|minutes|h|hr|hrs|hours)\b", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return f"every {n}{'h' if unit.startswith('h') else 'm'}", None

    if re.search(r"\b(hourly|every hour|each hour)\b", t):
        return "hourly", None

    m = re.search(r"\b(?:every|each)\s+(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", t)
    if m or re.search(r"\b(weekly|every week|each week)\b", t):
        clock = _clock(t)
        if not clock:
            return None, "what time of day it should run"
        if m:
            dow = _DOW_CANON[_DOW[m.group(1)]]
        else:
            dow_m = re.search(r"\bon\s+(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", t)
            if not dow_m:
                return None, "which day of the week it should run"
            dow = _DOW_CANON[_DOW[dow_m.group(1)]]
        return f"weekly on {dow} at {clock[0]:02d}:{clock[1]:02d}", None

    if re.search(r"\b(daily|every day|each day|every morning|each morning|"
                 r"every evening|every night|nightly)\b", t):
        clock = _clock(t)
        if not clock:
            return None, "what time of day it should run"
        return f"daily at {clock[0]:02d}:{clock[1]:02d}", None

    return None, None


def parse(text: str, known_agents: list[dict] | None = None) -> dict[str, Any]:
    """Understand a scheduling request without asking a model.

    Returns:
      recurring   — whether a cadence was asked for at all
      cadence     — a string suni.schedules accepts, or None
      missing     — list of details to ask the user for, in the order to ask
      email_to    — address if the user gave one
      wants_email — they asked for delivery by mail
      agent_slug  — resolved only against agents that were passed in
      agent_named — the name they used, whether or not it resolved
    """
    cadence, missing_reason = parse_cadence_phrase(text)
    t = text.lower()
    wants_email = bool(re.search(r"\b(e-?mail|mail me|send it to me|inbox)\b", t))
    email_m = _EMAIL.search(text)

    out: dict[str, Any] = {
        "recurring": bool(cadence or missing_reason),
        "cadence": cadence,
        "missing": [],
        "email_to": email_m.group(0) if email_m else "",
        "wants_email": wants_email,
        "agent_slug": "",
        "agent_named": "",
    }
    if missing_reason:
        out["missing"].append(missing_reason)

    # An address is required only because they asked for delivery. Never guess:
    # a wrong address means a daily leak of whatever the run produces.
    if wants_email and not out["email_to"]:
        out["missing"].append("which email address to send it to")

    # Agent name: prefer a quoted phrase, else "the X agent".
    named = ""
    q = _QUOTED.search(text)
    if q and re.search(r"\bagent\b", q.group(1), re.IGNORECASE):
        named = q.group(1).strip()
    if not named:
        m = re.search(r"\b(?:the\s+)?([A-Z][\w-]*(?:\s+[A-Z][\w-]*){0,3}\s+agent)\b", text)
        if m:
            named = m.group(1).strip()
    if named:
        out["agent_named"] = named
        want = named.lower()
        for a in (known_agents or []):
            if a["slug"].lower() == want or a["name"].strip().lower() == want:
                out["agent_slug"] = a["slug"]
                break
        else:
            loose = [a for a in (known_agents or [])
                     if want in a["name"].lower() or a["name"].lower() in want]
            if len(loose) == 1:
                out["agent_slug"] = loose[0]["slug"]
    return out


def question(parsed: dict[str, Any]) -> str | None:
    """The clarifying question to ask, or None when nothing is missing."""
    if not parsed["missing"]:
        return None
    if len(parsed["missing"]) == 1:
        return f"Before I set that up — {parsed['missing'][0]}?"
    bits = "; ".join(parsed["missing"][:-1]) + f"; and {parsed['missing'][-1]}"
    return f"Before I set that up, I need a couple of details — {bits}?"


_STRIP = [
    r"\b(?:every|each)\s+\d+\s*(?:m|min|mins|minutes|h|hr|hrs|hours)\b",
    r"\b(?:every|each)\s+(?:day|morning|evening|night|week|hour|"
    r"mon|tue|wed|thu|fri|sat|sun)[a-z]*\b",
    r"\b(?:daily|hourly|weekly|nightly)\b",
    r"\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
    r"\b(?:and\s+)?(?:deliver(?:ed)?|send(?:ing)?|sent|email(?:ed)?|mail(?:ed)?)\s+"
    r"(?:it\s+)?(?:to\s+)?(?:my\s+)?(?:e-?mail|inbox|me)\b",
    r"\bto\s+[\w.+-]+@[\w-]+\.[\w.-]+",
    r"[\w.+-]+@[\w-]+\.[\w.-]+",
    r"^\s*(?:suni[,:]?\s*)?(?:please\s+)?",
]


def strip_scheduling(text: str) -> str:
    """The task with its scheduling and delivery clauses removed.

    The stored prompt runs later with no conversation history and no user
    present. Leaving "email it to me every morning" inside it would make the
    scheduled run try to arrange its own delivery on top of the delivery the
    runner already performs.
    """
    out = text
    for pat in _STRIP:
        out = re.sub(pat, " ", out, flags=re.IGNORECASE)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,.;:")
    return out or text.strip()


def suggest_name(task: str) -> str:
    """A short label for the schedule list. Deliberately dull and predictable."""
    words = re.sub(r"[^\w\s-]", "", task).split()
    if not words:
        return "Scheduled run"
    label = " ".join(words[:6])
    return (label[:1].upper() + label[1:])[:60]


_DELEGATION_STRIP = [
    r"^\s*(?:suni[,:]?\s*)?(?:please\s+)?",
    r"\b(?:can you\s+)?(?:please\s+)?(?:ask|tell|get|have)\s+"
    r"(?:the\s+)?[\"'“‘]?[\w\s-]{0,40}?agent[\"'”’]?\s+(?:to\s+)?",
]


def strip_delegation(text: str) -> str:
    """The task with the "ask the X agent to" wrapper removed.

    The agent never sees this conversation, so it is handed the instruction
    itself rather than a request addressed to somebody else — otherwise it reads
    "ask the network agent to check the services" and starts looking for an
    agent of its own.
    """
    out = text
    for pat in _DELEGATION_STRIP:
        out = re.sub(pat, " ", out, count=1, flags=re.IGNORECASE)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,.;:")
    return out or text.strip()
