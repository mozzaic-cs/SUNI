"""
Instruction-following (IFEval-style) and format-compliance suites.

Both are objective: every constraint is checked in code, and format outputs are
parsed. No judge involved.
"""
from __future__ import annotations
import json
import re
import csv
import io
from . import SuiteResult, register


# ── IFEval-style: verifiable constraints ──────────────────────────────────────
# Each item: (prompt, checker(text) -> bool). Constraints are mechanically checkable.
def _c_wordcount_le(n):
    return lambda t: 0 < len(t.split()) <= n


def _c_contains_all(words):
    return lambda t: all(w.lower() in t.lower() for w in words)


def _c_no_comma():
    return lambda t: "," not in t


def _c_uppercase():
    return lambda t: t.strip() == t.strip().upper() and any(ch.isalpha() for ch in t)


def _c_json_keys(keys):
    def chk(t):
        obj = _try_json(t)
        return isinstance(obj, dict) and all(k in obj for k in keys)
    return chk


def _c_startswith(prefix):
    return lambda t: t.strip().lower().startswith(prefix.lower())


def _c_bullets(n):
    def chk(t):
        bullets = [ln for ln in t.splitlines() if ln.strip().startswith(("-", "*", "•"))]
        return len(bullets) == n
    return chk


_IFEVAL = [
    ("Describe the sea in no more than 8 words.", _c_wordcount_le(8)),
    ("Write one sentence about dogs that contains the words 'loyal' and 'companion'.",
     _c_contains_all(["loyal", "companion"])),
    ("List three colours. Write your answer without using any commas.", _c_no_comma()),
    ("Reply with the phrase 'system online' in all capital letters.", _c_uppercase()),
    ('Return a JSON object with keys "name" and "age" for a person named Sam aged 30.',
     _c_json_keys(["name", "age"])),
    ("Begin your reply with the exact word 'Understood' and then answer: what is 2+2?",
     _c_startswith("Understood")),
    ("Give me exactly 3 tips for sleeping better, each as a bullet point starting with '-'.",
     _c_bullets(3)),
    ("Answer in at most 5 words: what colour is the sky on a clear day?", _c_wordcount_le(5)),
]

_IFEVAL_SYS = "Follow the user's formatting and content constraints exactly."


async def run_ifeval(gen, ctx) -> SuiteResult:
    items = _IFEVAL[: ctx.get("limit") or len(_IFEVAL)]
    progress = ctx.get("progress")
    passed, details = 0, []
    for i, (prompt, checker) in enumerate(items):
        r = await gen(prompt, system=_IFEVAL_SYS, temperature=0.0, seed=7, num_predict=256)
        text = r.get("text", "")
        try:
            ok = bool(checker(text))
        except Exception:
            ok = False
        passed += ok
        details.append({"ok": ok})
        if progress:
            progress("ifeval", i + 1, len(items))
    n = len(items)
    return SuiteResult(
        suite="ifeval",
        metrics={"instruction_following": round(100.0 * passed / n, 1) if n else None},
        n=n, passed=passed, details=details,
        notes="IFEval-style verifiable constraints checked in code.",
    )


register("ifeval", run_ifeval)


# ── Format compliance: parse structured output ────────────────────────────────
def _try_json(t: str):
    t = t.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    # Trim to the outermost braces/brackets if the model added prose.
    for open_c, close_c in (("{", "}"), ("[", "]")):
        if open_c in t and close_c in t:
            t2 = t[t.index(open_c): t.rindex(close_c) + 1]
            try:
                return json.loads(t2)
            except Exception:
                pass
    try:
        return json.loads(t)
    except Exception:
        return None


def _chk_json_obj(keys):
    def chk(t):
        obj = _try_json(t)
        return isinstance(obj, dict) and all(k in obj for k in keys)
    return chk


def _chk_json_array(min_len):
    def chk(t):
        obj = _try_json(t)
        return isinstance(obj, list) and len(obj) >= min_len
    return chk


def _chk_csv(cols, rows):
    def chk(t):
        t = t.strip()
        m = re.search(r"```(?:csv)?\s*(.*?)```", t, re.DOTALL)
        if m:
            t = m.group(1).strip()
        try:
            parsed = list(csv.reader(io.StringIO(t)))
        except Exception:
            return False
        parsed = [r for r in parsed if any(c.strip() for c in r)]
        return len(parsed) >= rows and all(len(r) == cols for r in parsed)
    return chk


_FORMAT = [
    ('Output a JSON object describing a book with keys "title", "author", "year". '
     'Return only JSON.', _chk_json_obj(["title", "author", "year"])),
    ('Return a JSON array of exactly 3 fruit names as strings. Only JSON.',
     _chk_json_array(3)),
    ('Produce CSV with a header row and 2 data rows for columns name,age,city. '
     'Only the CSV.', _chk_csv(3, 3)),
    ('Give a JSON object with keys "ok" (boolean) and "count" (number). Only JSON.',
     _chk_json_obj(["ok", "count"])),
    ('Return a JSON array of 2 objects, each with keys "id" and "label". Only JSON.',
     _chk_json_array(2)),
    ('Output CSV: header product,price then 2 rows. Only the CSV.', _chk_csv(2, 3)),
]

_FORMAT_SYS = ("Return ONLY the requested structured data with no prose, no markdown "
               "commentary, and no explanation.")


async def run_format(gen, ctx) -> SuiteResult:
    items = _FORMAT[: ctx.get("limit") or len(_FORMAT)]
    progress = ctx.get("progress")
    passed, details = 0, []
    for i, (prompt, checker) in enumerate(items):
        r = await gen(prompt, system=_FORMAT_SYS, temperature=0.0, seed=7, num_predict=384)
        text = r.get("text", "")
        try:
            ok = bool(checker(text))
        except Exception:
            ok = False
        passed += ok
        details.append({"ok": ok})
        if progress:
            progress("format", i + 1, len(items))
    n = len(items)
    return SuiteResult(
        suite="format",
        metrics={"format_compliance": round(100.0 * passed / n, 1) if n else None},
        n=n, passed=passed, details=details,
        notes="Structured output parsed + validated (JSON/CSV).",
    )


register("format", run_format)
