"""
Checks on mail arriving in SUNI's own mailbox, for anything strange.

Deterministic on purpose: these run on every message the watcher sees and
their only output is an alert to the admin. Nothing here acts on a message,
replies to it, or passes it to a model — a finding is a report, never a
decision (rule 2: no instruction is ever accepted by email, whoever sends it).

A sender's identity grants nothing either way. From: is trivially forged, so
mail that claims to come from the admin is checked exactly like any other —
and mail claiming to be from SUNI or the admin from a foreign domain is itself
one of the things reported.
"""
from __future__ import annotations
import re
import time
from collections import defaultdict, deque
from email.message import Message
from email.utils import getaddresses, parseaddr

# ── what the checks look for ─────────────────────────────────────────────────

# Attachments that run code when opened, or carry macros.
_RISKY_EXT = {
    ".exe", ".scr", ".com", ".pif", ".bat", ".cmd", ".ps1", ".vbs", ".vbe",
    ".js", ".jse", ".wsf", ".wsh", ".hta", ".msi", ".msp", ".jar", ".lnk",
    ".reg", ".dll", ".cpl", ".iso", ".img", ".vhd", ".application",
    ".docm", ".xlsm", ".pptm", ".dotm", ".xltm", ".xlam", ".ppam",
}

# Phrasing aimed at an AI that reads mail, rather than at a person. A person
# writing "please send me the report" is ordinary mail and is NOT matched:
# these are the shapes of prompt injection — overriding instructions, asking
# for secrets, or telling "the assistant" what to do with the mailbox.
# English and pt-PT, because the intent regexes elsewhere were English-only
# and missed Portuguese entirely.
_INJECTION = [re.compile(p, re.IGNORECASE) for p in (
    r"\bignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier|your)\s+(instructions|rules|prompts?)",
    r"\bdisregard\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|your)\b",
    r"\b(new|updated|override)\s+(system\s+)?instructions?\s*:",
    r"\bsystem\s+prompt\b",
    r"\byou\s+are\s+now\s+(a|an|in)\b",
    r"\b(as|dear)\s+(an?\s+)?(ai|assistant|language model|llm)\b",
    r"\b(suni|assistant|ai)\s*[,:]\s*(please\s+)?(send|forward|reply|delete|run|execute|transfer|share)\b",
    r"\b(forward|send)\s+(all|every|the\s+last)\s+(\w+\s+)?(emails?|messages?|files?)\b",
    r"\b(api[\s_-]?key|password|passwd|access\s+token|credentials?)\b.{0,40}\b(send|reply|share|give)\b",
    r"\b(send|reply|share|give)\b.{0,40}\b(api[\s_-]?key|password|access\s+token|credentials?)\b",
    r"</?(system|instructions?|prompt)>",
    # pt-PT
    r"\bignor[ae]\s+(todas\s+)?as\s+instru[cç][oõ]es\b",
    r"\binstru[cç][oõ]es\s+anteriores\b",
    r"\bnovas\s+instru[cç][oõ]es\s*:",
    r"\b(a\s+partir\s+de\s+agora\s+)?(tu\s+)?[eé]s\s+agora\s+(um|uma)\b",
    r"\b(suni|assistente|ia)\s*[,:]\s*(por\s+favor\s+)?(envia|reencaminha|responde|apaga|executa|transfere|partilha)\b",
    r"\breencaminha\s+(todos|todas|os\s+[uú]ltimos|as\s+[uú]ltimas)\b",
    r"\b(palavra[\s-]passe|senha|chave\s+(de\s+)?api|credenciais)\b.{0,40}\b(envia|responde|partilha|d[aá]-me)\b",
    r"\b(envia|responde|partilha|d[aá]-me)\b.{0,40}\b(palavra[\s-]passe|senha|chave\s+(de\s+)?api|credenciais)\b",
)]

# Webmail domains: an admin on gmail.com says nothing about other gmail.com
# senders, and ymail/mail.com sit one letter away — comparing against these
# would report half the internet as a lookalike.
_FREEMAIL = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "yahoo.com", "ymail.com", "icloud.com", "me.com", "aol.com",
    "proton.me", "protonmail.com", "gmx.com", "gmx.net", "mail.com",
    "sapo.pt", "outlook.pt", "hotmail.pt", "yahoo.com.br", "zoho.com",
}

# One sender this many times inside the window is a flood, not correspondence.
FLOOD_COUNT = 8
FLOOD_WINDOW_S = 3600


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].strip().lower() if "@" in addr else ""


def _trusted_domains(domains) -> set[str]:
    """Domains whose lookalikes are worth reporting: SUNI's and the admins',
    minus webmail (see _FREEMAIL)."""
    return {d for d in domains if d and d not in _FREEMAIL}


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _lookalike(dom: str, trusted: set[str]) -> str:
    """The trusted domain `dom` imitates, or ''. Punycode is reported on its
    own: an internationalised domain imitating a Latin one is the usual case."""
    if not dom or dom in trusted or any(dom.endswith("." + t) for t in trusted if t):
        return ""
    for t in trusted:
        if not t:
            continue
        base_d, base_t = dom.split(".")[0], t.split(".")[0]
        if _levenshtein(dom, t) <= 2 or (len(base_t) >= 4 and base_d != base_t
                                          and _levenshtein(base_d, base_t) <= 1):
            return t
        if base_t in dom and dom != t and len(base_t) >= 4:
            return t                       # acme.test.evil.example, acme-mail.example
    return ""


def _auth_failures(msg: Message) -> list[str]:
    """SPF / DKIM / DMARC verdicts the receiving server recorded. Only the
    receiving server's own Authentication-Results header is meaningful, and a
    sender can add fake ones below it, so only the FIRST (topmost) is read."""
    hdr = msg.get_all("Authentication-Results") or []
    if not hdr:
        return []
    top = str(hdr[0]).lower()
    out = []
    for mech in ("spf", "dkim", "dmarc"):
        m = re.search(rf"\b{mech}\s*=\s*(\w+)", top)
        if m and m.group(1) in ("fail", "softfail", "permerror"):
            out.append(f"{mech}={m.group(1)}")
    return out


def _attachment_names(msg: Message) -> list[str]:
    names = []
    for part in msg.walk():
        fn = part.get_filename()
        if fn:
            names.append(str(fn))
    return names


def injection_hits(text: str) -> list[str]:
    """The phrases in `text` that read as instructions aimed at an AI."""
    hits = []
    for rx in _INJECTION:
        m = rx.search(text or "")
        if m:
            hits.append(m.group(0)[:60])
    return hits


class SenderWindow:
    """Messages per sender in the last FLOOD_WINDOW_S seconds. In memory: a
    restart forgets the window, which at worst delays one flood alert."""

    def __init__(self) -> None:
        self._seen: dict[str, deque] = defaultdict(deque)

    def add(self, sender: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        q = self._seen[sender]
        q.append(now)
        while q and now - q[0] > FLOOD_WINDOW_S:
            q.popleft()
        return len(q)


def assess(msg: Message, *, own_address: str, admin_addresses: list[str] | None = None,
           sent_message_ids: set[str] | None = None, body_text: str = "",
           window: SenderWindow | None = None) -> list[dict]:
    """Everything strange about one inbound message, as
    [{"code", "detail"}]. Empty means nothing was found."""
    flags: list[dict] = []
    add = lambda code, detail: flags.append({"code": code, "detail": detail})

    name, addr = parseaddr(str(msg.get("From", "")))
    addr = addr.lower()
    dom = _domain(addr)
    own = (own_address or "").lower()
    own_dom = _domain(own)
    admins = [a.lower() for a in (admin_addresses or []) if a]
    trusted_doms = _trusted_domains({own_dom, *(_domain(a) for a in admins)})

    for f in _auth_failures(msg):
        add("auth_fail", f"sender authentication failed ({f})")

    # Mail claiming to be from SUNI herself or from an admin. Even a correct
    # address proves nothing (From: is forged freely) — so a claim of SUNI's
    # own address on inbound mail is reported, since she never mails herself.
    if own and addr == own:
        add("spoof_self", f"claims to be from SUNI's own address ({addr})")
    nm = (name or "").lower()
    if nm and addr != own and addr not in admins and (
            re.search(r"\bsuni\b", nm)
            or any(len(a.split("@")[0]) >= 4 and a.split("@")[0] in nm for a in admins)):
        add("display_name", f"display name \"{name}\" on another address ({addr})")

    imitated = _lookalike(dom, trusted_doms)
    if imitated:
        add("lookalike", f"domain {dom} resembles {imitated}")
    if dom.startswith("xn--") or ".xn--" in dom:
        add("punycode", f"internationalised domain {dom}")

    reply_to = [a.lower() for _, a in getaddresses(msg.get_all("Reply-To") or []) if a]
    if reply_to and any(_domain(r) != dom for r in reply_to):
        add("reply_to", f"replies would go to {', '.join(reply_to)}, not the sender")

    risky = [n for n in _attachment_names(msg)
             if any(n.lower().endswith(e) for e in _RISKY_EXT)
             or re.search(r"\.(pdf|docx?|xlsx?|jpg|png|txt)\.\w{2,4}$", n.lower())]
    if risky:
        add("attachment", f"executable or macro attachment: {', '.join(risky)[:120]}")

    # A reply to a thread SUNI never wrote in: a common way to look legitimate.
    if sent_message_ids is not None:
        refs = " ".join(str(msg.get(h, "")) for h in ("In-Reply-To", "References"))
        ids = set(re.findall(r"<[^<>\s]+>", refs))
        subj = str(msg.get("Subject", "")).strip().lower()
        if (ids and not (ids & sent_message_ids)) or (not ids and re.match(r"^(re|res|fw|fwd|enc)\s*:", subj)):
            add("unknown_thread", "a reply to a conversation SUNI never sent")

    hits = injection_hits(f"{msg.get('Subject', '')}\n{body_text}")
    if hits:
        add("instructions", "contains instructions aimed at an AI: " + "; ".join(f"\"{h}\"" for h in hits[:3]))

    if window is not None and addr:
        n = window.add(addr)
        if n >= FLOOD_COUNT:
            add("flood", f"{n} messages from {addr} in the last hour")

    return flags
