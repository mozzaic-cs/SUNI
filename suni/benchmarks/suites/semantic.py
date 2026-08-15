"""
Semantic suites using SUNI's own MiniLM embeddings (objective vector math).

  semantic_sim         mean cosine of answers vs reference answers (higher better).
  prompt_sensitivity   answer drift across equivalent paraphrases
                       (1 - mean pairwise cosine; lower = more stable).
  model_variability    answer drift across identical repeated runs at temp>0
                       (1 - mean pairwise cosine; lower = more consistent).
"""
from __future__ import annotations
from itertools import combinations
from . import SuiteResult, register, embed, cosine

_SYS = "Answer the question concisely and factually."


def _mean_pairwise_cos(texts: list[str]) -> float | None:
    texts = [t for t in texts if t and t.strip()]
    if len(texts) < 2:
        return None
    vecs = embed(texts)
    sims = [cosine(vecs[a], vecs[b]) for a, b in combinations(range(len(vecs)), 2)]
    return sum(sims) / len(sims) if sims else None


# ── semantic similarity vs reference ──────────────────────────────────────────
_QA = [
    ("What is the capital of France?", "The capital of France is Paris."),
    ("How many continents are there on Earth?", "There are seven continents."),
    ("What gas do plants absorb from the air for photosynthesis?",
     "Plants absorb carbon dioxide."),
    ("Who wrote the play Romeo and Juliet?", "William Shakespeare wrote Romeo and Juliet."),
    ("What is the boiling point of water at sea level in Celsius?",
     "Water boils at 100 degrees Celsius at sea level."),
    ("What is the largest planet in our solar system?", "Jupiter is the largest planet."),
]


async def run_semantic_sim(gen, ctx) -> SuiteResult:
    items = _QA[: ctx.get("limit") or len(_QA)]
    progress = ctx.get("progress")
    sims, details = [], []
    for i, (q, ref) in enumerate(items):
        r = await gen(q, system=_SYS, temperature=0.0, seed=7, num_predict=128)
        ans = r.get("text", "")
        v = embed([ans, ref]) if ans.strip() else None
        c = cosine(v[0], v[1]) if v is not None else 0.0
        sims.append(c)
        details.append({"cos": round(c, 3)})
        if progress:
            progress("semantic_sim", i + 1, len(items))
    val = round(sum(sims) / len(sims), 3) if sims else None
    return SuiteResult(
        suite="semantic_sim",
        metrics={"semantic_sim": val},
        n=len(items), passed=sum(1 for s in sims if s >= 0.6), details=details,
        notes="Cosine of answer vs reference (MiniLM embeddings).",
    )


register("semantic_sim", run_semantic_sim)


# ── prompt sensitivity (paraphrase drift) ─────────────────────────────────────
_PARAPHRASES = [
    ["What is the capital of Japan?",
     "Which city is the capital of Japan?",
     "Tell me the name of Japan's capital city.",
     "Japan's capital is which city?"],
    ["How many legs does a spider have?",
     "What is the number of legs on a spider?",
     "A spider has how many legs?"],
    ["What is 15 multiplied by 4?",
     "Compute the product of 15 and 4.",
     "What do you get when you multiply 15 by 4?"],
]


async def run_prompt_sensitivity(gen, ctx) -> SuiteResult:
    sets = _PARAPHRASES[: ctx.get("limit") or len(_PARAPHRASES)]
    progress = ctx.get("progress")
    instabilities, details = [], []
    for i, para in enumerate(sets):
        answers = []
        for p in para:
            r = await gen(p, system=_SYS, temperature=0.0, seed=7, num_predict=96)
            answers.append(r.get("text", ""))
        mc = _mean_pairwise_cos(answers)
        inst = round(1 - mc, 3) if mc is not None else None
        if inst is not None:
            instabilities.append(inst)
        details.append({"instability": inst})
        if progress:
            progress("prompt_sensitivity", i + 1, len(sets))
    val = round(sum(instabilities) / len(instabilities), 3) if instabilities else None
    return SuiteResult(
        suite="prompt_sensitivity",
        metrics={"prompt_sensitivity": val},
        n=len(sets), passed=0, details=details,
        notes="1 - mean pairwise cosine across paraphrases (lower = more stable).",
    )


register("prompt_sensitivity", run_prompt_sensitivity)


# ── model variability (repeat-run drift) ──────────────────────────────────────
_VAR_PROMPTS = [
    "Write a one-sentence description of a sunset.",
    "Suggest a name for a friendly robot.",
    "Give a short tip for staying focused at work.",
]


async def run_variability(gen, ctx) -> SuiteResult:
    prompts = _VAR_PROMPTS[: ctx.get("limit") or len(_VAR_PROMPTS)]
    progress = ctx.get("progress")
    runs = 4
    instabilities, details = [], []
    for i, p in enumerate(prompts):
        answers = []
        for k in range(runs):
            # temp>0 and a different seed per run to measure genuine run-to-run drift
            r = await gen(p, system=_SYS, temperature=0.8, seed=100 + k, num_predict=96)
            answers.append(r.get("text", ""))
        mc = _mean_pairwise_cos(answers)
        inst = round(1 - mc, 3) if mc is not None else None
        if inst is not None:
            instabilities.append(inst)
        details.append({"instability": inst})
        if progress:
            progress("variability", i + 1, len(prompts))
    val = round(sum(instabilities) / len(instabilities), 3) if instabilities else None
    return SuiteResult(
        suite="variability",
        metrics={"model_variability": val},
        n=len(prompts), passed=0, details=details,
        notes=f"1 - mean pairwise cosine over {runs} runs at temp 0.8 (lower = more consistent).",
    )


register("variability", run_variability)
