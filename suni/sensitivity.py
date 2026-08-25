"""
Detecting personal data, credentials and injection attempts in text that is
about to become shared memory.

This exists because `sensitivity` was a lie. The promotion endpoint wrote
`"sensitivity": "normal"` on every fact regardless of content, and nothing read
it back — a governance field that was always wrong and reached nothing. The
detector gives it a real value, and the endpoint now acts on it.

**Deterministic, not a model call.** An LLM classifier per extraction would be
nondeterministic, slow, and — on a 7B with roughly 1,600 usable tokens after the
tool schemas and prompt — unaffordable. Patterns over fixed strings can be
tested against exact inputs, which is what makes the negative cases below
possible at all.

**Check digits, not shapes.** This is the whole reason the detector can be
trusted. A Portuguese NIF is nine digits; so is an invoice number, an order
reference and a product code. Matching the shape would flag all of them, and
`reference_release_scan_gate.md` records what happens next: "a noisy gate gets
ignored and that is how it went blind." Validating the checksum means an invoice
number is not a NIF unless it happens to satisfy mod-11 — roughly a one-in-eleven
coincidence rather than a certainty.

**Generic patterns, not known values.** The same post-mortem: the release gate
found credentials only by grepping for values it had already read off disk, so a
password typed straight into source was invisible. Here the equivalent mistake
would be looking only for SUNI's own secrets. The credential rules match the
*shape of an assignment*, not any particular value.

Two traps from that post-mortem are reproduced deliberately:
  - `\\b` never matches after an underscore, so `\\bpass` misses `SMTP_PASS`.
    The credential patterns use an explicit `[^a-z0-9]` boundary instead.
  - "A bare identifier is a variable, not a literal" silently re-blinded the
    gate, because a real password is indistinguishable from an identifier. Call
    syntax is the tell for code, not the absence of punctuation.
"""
from __future__ import annotations

import re
from typing import Any

# Ordered least → most sensitive. `normal` is the only value that may be
# auto-approved into shared memory.
LEVELS = ("normal", "pii", "confidential")


def _rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


# ── check-digit validators ───────────────────────────────────────────────────
def _nif_valid(digits: str) -> bool:
    """Portuguese tax number: 9 digits, mod-11 check digit.

    The first digit also carries meaning (1/2/3 individuals, 5 companies, and
    so on); a leading 0 or 4 is never a NIF. Checking it costs nothing and
    removes a whole class of false positive.
    """
    if len(digits) != 9 or not digits.isdigit():
        return False
    if digits[0] not in "1235689":
        return False
    total = sum(int(digits[i]) * (9 - i) for i in range(8))
    rem = total % 11
    check = 0 if rem < 2 else 11 - rem
    return check == int(digits[8])


def _niss_valid(digits: str) -> bool:
    """Portuguese social security number: 11 digits, weighted mod-10."""
    if len(digits) != 11 or not digits.isdigit():
        return False
    if digits[0] not in "12":
        return False
    weights = (29, 23, 19, 17, 13, 11, 7, 5, 3, 2)
    total = sum(int(digits[i]) * weights[i] for i in range(10))
    return (9 - (total % 10)) == int(digits[10])


def _luhn_valid(s: str) -> bool:
    """Mod-10, used by payment cards and by the Cartão de Cidadão."""
    total, double = 0, False
    for ch in reversed(s):
        if ch.isdigit():
            v = int(ch)
        elif ch.isalpha():
            v = ord(ch.upper()) - 55        # A=10 … Z=35
        else:
            return False
        if double:
            v *= 2
            if v > 9:
                v -= 9
        total += v
        double = not double
    return total % 10 == 0


def _iban_valid(s: str) -> bool:
    """ISO 13616 mod-97. Covers PT50… and every other country's form."""
    s = re.sub(r"[\s-]", "", s).upper()
    if len(s) < 15 or len(s) > 34 or not s[:2].isalpha() or not s[2:4].isdigit():
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    if not digits.isdigit():
        return False
    return int(digits) % 97 == 1


# ── candidate extractors ─────────────────────────────────────────────────────
_RUN_9 = re.compile(r"(?<![\d-])(\d[\d ]{7,10}\d)(?![\d-])")
# Allows the printed 4-character grouped form ("PT50 0002 0123 …"). An earlier
# version put \s in the character class, which let the match run on into the
# words after the IBAN and then fail validation — the IBAN was right there and
# went undetected, which is the worst of both outcomes.
_IBAN_LIKE = re.compile(r"\b([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4})+[ ]?[A-Z0-9]{0,3})\b")
_CARD_LIKE = re.compile(r"(?<![\d-])((?:\d[ -]?){12,18}\d)(?![\d-])")
_CC_LIKE = re.compile(r"\b(\d{8}[\s-]?\d[\s-]?[A-Z]{2}[\s-]?\d)\b")
_US_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EMAIL = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE_INTL = re.compile(r"\+\d{1,3}[\s.-]?\d[\d\s.-]{6,14}\d")

# Credentials. `[^a-z0-9]` rather than \b — see the module docstring.
_CRED_ASSIGN = re.compile(
    # bare "pass" belongs here: SUNI_SMTP_PASS is exactly the shape the release
    # scan gate missed. The trailing [:=] requirement keeps "passed the test"
    # from matching.
    r"(?:^|[^a-z0-9])(password|passwd|pass|pwd|secret|api[_-]?key|apikey|token|"
    r"auth|credential)s?\s*[:=]\s*[\"']?([^\s\"',;]{6,})",
    re.I)
_CRED_IN_URI = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:([^\s/@]{3,})@", re.I)
_CONN_STRING = re.compile(r"(?:^|[;\s])(pwd|password|user\s*id|uid)\s*=\s*([^;\s]{3,})", re.I)
_VENDOR_TOKEN = re.compile(
    r"\b(?:xox[baprs]-[\w-]{10,}|sk-[A-Za-z0-9]{20,}|sk-ant-[\w-]{10,}|"
    r"ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{30,})")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

# Prompt-injection shapes. The proposal calls these out because aggregated
# activity memory is the highest-risk injection source in the system: text a
# user wrote becomes text the model reads as context, for everyone.
_INJECTION = re.compile(
    r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+instructions?|"
    r"disregard\s+(?:all\s+|the\s+)?(?:previous|prior|above)\s+|"
    r"you\s+are\s+now\s+(?:a|an|the)\s|"
    r"forget\s+(?:everything|all)\s+(?:you|above|before)|"
    r"new\s+system\s+prompt|"
    r"</?(?:system|instructions?)>|"
    r"\[/?(?:INST|SYSTEM)\]",
    re.I)

# Call syntax is the reliable tell that a "credential" is code, not a value —
# `get_password(user)` is not a leak. Prose placeholders are excluded too.
_LOOKS_LIKE_CODE = re.compile(r"[()\[\]{}]|\+\s*\w|\bself\.|\bos\.environ|getenv", re.I)
_PLACEHOLDER = re.compile(
    r"^(?:\*+|x+|\.+|-+|<[^>]*>|\{[^}]*\}|your[_-]?\w*|my[_-]?\w*|"
    r"changeme|example|placeholder|redacted|none|null|nil|todo|tbd|"
    r"password|secret|token|xxx+|\.\.\.)$", re.I)


def _clean_digits(s: str) -> str:
    return re.sub(r"[\s-]", "", s)


def classify(text: Any) -> dict:
    """Return {sensitivity, reasons, injection} for a piece of candidate memory.

    `reasons` names what matched, never what it matched — putting the detected
    value into a return that gets logged or shown in a review queue would leak
    the thing being protected.
    """
    out = {"sensitivity": "normal", "reasons": [], "injection": False}
    s = str(text or "")
    if not s.strip():
        return out

    def flag(level: str, reason: str) -> None:
        if reason not in out["reasons"]:
            out["reasons"].append(reason)
        if _rank(level) > _rank(out["sensitivity"]):
            out["sensitivity"] = level

    # ── credentials → confidential ───────────────────────────────────────────
    if _PRIVATE_KEY.search(s):
        flag("confidential", "private-key")
    if _VENDOR_TOKEN.search(s):
        flag("confidential", "vendor-token")
    if _CRED_IN_URI.search(s):
        flag("confidential", "credential-in-uri")
    for m in _CONN_STRING.finditer(s):
        # Same code/placeholder suppression as the assignment rule below. Without
        # it this rule fired on `password=get_password(user)` — the assignment
        # rule correctly skipped it and this one flagged it anyway.
        value = m.group(2).strip("\"'")
        if _LOOKS_LIKE_CODE.search(value) or _PLACEHOLDER.match(value):
            continue
        flag("confidential", "credential-in-connection-string")
    for m in _CRED_ASSIGN.finditer(s):
        value = m.group(2).strip("\"'")
        # Skip obvious code (`password=get_pw()`) and prose placeholders.
        if _LOOKS_LIKE_CODE.search(value) or _PLACEHOLDER.match(value):
            continue
        flag("confidential", "credential-assignment")

    # ── personal identifiers → pii ───────────────────────────────────────────
    for m in _IBAN_LIKE.finditer(s.upper()):
        if _iban_valid(m.group(1)):
            flag("pii", "iban")
    for m in _CARD_LIKE.finditer(s):
        d = _clean_digits(m.group(1))
        # A run of identical digits passes Luhn — sixteen zeros sum to zero —
        # but it is a serial number or a redaction, not a card.
        if 13 <= len(d) <= 19 and len(set(d)) > 1 and _luhn_valid(d):
            flag("pii", "payment-card")
    for m in _CC_LIKE.finditer(s.upper()):
        raw = re.sub(r"[\s-]", "", m.group(1))
        if len(raw) == 12 and _luhn_valid(raw):
            flag("pii", "pt-cartao-de-cidadao")
    for m in _RUN_9.finditer(s):
        d = _clean_digits(m.group(1))
        if _nif_valid(d):
            flag("pii", "pt-nif")
        elif _niss_valid(d):
            flag("pii", "pt-niss")
    if _US_SSN.search(s):
        flag("pii", "us-ssn")
    if _EMAIL.search(s):
        flag("pii", "email-address")
    if _PHONE_INTL.search(s):
        flag("pii", "phone-number")

    # ── injection shapes ─────────────────────────────────────────────────────
    if _INJECTION.search(s):
        out["injection"] = True
        flag("confidential", "injection-shape")

    return out


def may_auto_approve(text: Any) -> tuple[bool, dict]:
    """Whether this content may go straight into shared memory.

    Default-deny, per §9.2 of the governance design: anything that is not
    plainly `normal` is staged as a candidate for a human instead. The verdict
    is returned with its findings so the caller can record why.
    """
    verdict = classify(text)
    ok = verdict["sensitivity"] == "normal" and not verdict["injection"]
    return ok, verdict
