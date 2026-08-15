"""
RULER-style long-context retrieval. Objective: exact-match a needle placed at
varying depths inside a large filler haystack.
"""
from __future__ import annotations
import re
from . import SuiteResult, register

_FILLER = ("The archives contain routine maintenance notes about the facility. "
           "Nothing of importance is recorded in this particular passage. ")

_NEEDLE_TMPL = "IMPORTANT: the secret magic number for sector {d} is {code}."

_SYS = ("You are given a long document. Read it carefully and answer the question "
        "using only the information stated in the document. Reply with the number only.")


def _build(depth_frac: float, target_words: int, code: str, sector: int):
    """Return (haystack_text, code). Needle placed at the given depth fraction."""
    needle = _NEEDLE_TMPL.format(d=sector, code=code)
    filler_words = _FILLER.split()
    total = []
    while len(total) < target_words:
        total.extend(filler_words)
    insert_at = int(len(total) * depth_frac)
    words = total[:insert_at] + needle.split() + total[insert_at:]
    return " ".join(words), code


async def run(gen, ctx) -> SuiteResult:
    num_ctx = ctx.get("num_ctx") or 8192
    progress = ctx.get("progress")
    # Target haystack ~55% of context (leave room for prompt+answer). Word≈token proxy.
    target_words = max(200, int(num_ctx * 0.55))
    # cap runtime for very large contexts
    target_words = min(target_words, 6000)

    cases = [(0.1, "4827", 3), (0.5, "9153", 7), (0.9, "6402", 11)]
    cases = cases[: ctx.get("limit") or len(cases)]

    passed, details = 0, []
    for i, (depth, code, sector) in enumerate(cases):
        hay, gold = _build(depth, target_words, code, sector)
        q = (f"{hay}\n\nQuestion: what is the secret magic number for sector {sector}? "
             f"Answer with the number only.")
        r = await gen(q, system=_SYS, temperature=0.0, seed=7, num_predict=32)
        nums = re.findall(r"\d+", r.get("text", ""))
        ok = gold in nums
        passed += ok
        details.append({"depth": depth, "gold": gold, "ok": ok, "approx_words": target_words})
        if progress:
            progress("ruler", i + 1, len(cases))
    n = len(cases)
    return SuiteResult(
        suite="ruler",
        metrics={"ruler": round(100.0 * passed / n, 1) if n else None},
        n=n, passed=passed, details=details,
        notes=f"Needle-in-haystack (~{target_words} words) at depths 10/50/90%.",
    )


register("ruler", run)
