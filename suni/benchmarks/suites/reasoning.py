"""GSM8K — multi-step grade-school math. Objective: exact-match the final number."""
from __future__ import annotations
import re
from . import SuiteResult, register

# Indicative bundled subset (not the official 8.5k set). Answers are exact integers.
_ITEMS = [
    ("Natalia sold clips to 48 friends in April, then sold half as many in May. "
     "How many clips did she sell altogether?", 72),
    ("Weng earns $12 an hour for babysitting. Yesterday she babysat for 50 minutes. "
     "How much did she earn, in dollars?", 10),
    ("Betty is saving for a $100 wallet. She has half the money she needs. Her parents "
     "give her $15 and her grandparents twice as much as her parents. How much more money "
     "does Betty need to buy the wallet?", 5),
    ("A robe takes 2 bolts of blue fibre and half that much white fibre. "
     "How many bolts in total does it take?", 3),
    ("James writes a 3-page letter to 2 friends twice a week. "
     "How many pages does he write a year?", 624),
    ("There are 15 trees in the grove. Workers will plant trees so that there are 21 "
     "trees when done. How many trees did the workers plant?", 6),
    ("If there are 3 cars in the lot and 2 more arrive, how many cars are in the lot?", 5),
    ("Leah had 32 chocolates and her sister had 42. If they ate 35, how many pieces do "
     "they have left in total?", 39),
    ("Jason had 20 lollipops. He gave Denny some. Now Jason has 12 lollipops. "
     "How many lollipops did Jason give to Denny?", 8),
    ("Sam has 4 boxes with 6 pencils each and buys 3 more pencils. "
     "How many pencils does Sam have?", 27),
]

_SYS = ("You are solving a math word problem. Think step by step, then end your reply "
        "with a line of the form '#### <number>' giving the final numeric answer only.")

_NUM = re.compile(r"-?\d[\d,]*")


def _extract(text: str):
    # Prefer the GSM8K-style '#### N' marker; fall back to the last number seen.
    m = re.search(r"####\s*(-?\d[\d,]*)", text)
    if m:
        return _to_int(m.group(1))
    nums = _NUM.findall(text)
    return _to_int(nums[-1]) if nums else None


def _to_int(s: str):
    try:
        return int(s.replace(",", ""))
    except Exception:
        return None


async def run(gen, ctx) -> SuiteResult:
    items = _ITEMS[: ctx.get("limit") or len(_ITEMS)]
    progress = ctx.get("progress")
    passed, details = 0, []
    for i, (q, gold) in enumerate(items):
        r = await gen(q, system=_SYS, temperature=0.0, seed=7, num_predict=512)
        got = _extract(r.get("text", ""))
        ok = got == gold
        passed += ok
        details.append({"gold": gold, "got": got, "ok": ok})
        if progress:
            progress("gsm8k", i + 1, len(items))
    n = len(items)
    return SuiteResult(
        suite="gsm8k",
        metrics={"gsm8k": round(100.0 * passed / n, 1) if n else None},
        n=n, passed=passed, details=details,
        notes="Indicative 10-item subset; exact-match on final integer.",
    )


register("gsm8k", run)
