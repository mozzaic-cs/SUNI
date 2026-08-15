"""
The 33-metric registry — the backbone of the benchmark dashboard.

Each metric is defined once here with everything the UI and the runner need:
its category, how it is sourced, its unit, and (for on-demand suites) the
suite key that produces it. This module holds NO measurements — it is a static
description of *what* SUNI tracks and *how trustworthy* each number is.

Source semantics
----------------
  live       Passive telemetry from real inference (telemetry.py). Continuous.
  on_demand  Produced by a benchmark suite run (runner.py + suites/). Stale
             until a run is triggered; carries the run timestamp.
  estimated  Derived from model/hardware config, not a measurement (e.g. cost,
             parameter count). Honest but approximate.
  na         No trustworthy local measurement exists. Always paired with a
             reason and never shown as a number.

Confidence (on_demand only)
  high       Objectively scored — numeric match, test execution, regex, parse,
             attack-success detection. No model judges itself.
  low        Needs an independent judge SUNI does not have locally. Reserved
             for a future grader; shipped as na until one exists.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict

# ── source / confidence constants ────────────────────────────────────────────
LIVE = "live"
ON_DEMAND = "on_demand"
ESTIMATED = "estimated"
NA = "na"

HIGH = "high"
LOW = "low"

# ── categories (mirror the reference article's grouping) ──────────────────────
CAT_PERF = "Performance & Speed"
CAT_COST = "Cost"
CAT_ARCH = "Model Architecture"
CAT_ACCURACY = "Accuracy & Hallucination"
CAT_SEMANTIC = "Semantic & Contextual"
CAT_FORMAT = "Format & Instruction Following"
CAT_AGENT = "Agent Behaviour"
CAT_SAFETY = "Safety & Security"
CAT_CONTEXT = "Context Understanding"
CAT_BENCH = "Standardised Benchmarks"

CATEGORY_ORDER = [
    CAT_PERF, CAT_COST, CAT_ARCH, CAT_ACCURACY, CAT_SEMANTIC,
    CAT_FORMAT, CAT_AGENT, CAT_SAFETY, CAT_CONTEXT, CAT_BENCH,
]


@dataclass
class Metric:
    id: str                     # stable key, used by telemetry / suites / UI
    name: str                   # display name (matches the article)
    category: str
    source: str                 # LIVE | ON_DEMAND | ESTIMATED | NA
    unit: str = ""              # "ms", "tok/s", "%", "$", "" …
    higher_is_better: bool | None = None
    desc: str = ""              # what it measures (one line)
    method: str = ""            # how SUNI computes it (one line)
    suite: str = ""             # on_demand: key of the suite that produces it
    confidence: str = HIGH      # on_demand only: HIGH | LOW
    na_reason: str = ""         # na only: why no local number

    def to_dict(self) -> dict:
        return asdict(self)


# ──────────────────────────────────────────────────────────────────────────────
# The 33 metrics, in the article's order. `id`s are what telemetry.py and the
# suites emit; keep them stable.
# ──────────────────────────────────────────────────────────────────────────────
METRICS: list[Metric] = [
    # ── 1-7  Performance & Speed ──────────────────────────────────────────────
    Metric("ttft", "Time to First Token", CAT_PERF, LIVE, "ms", False,
           "Initial response latency before any token is produced.",
           "Proxy from Ollama load_duration + prompt_eval_duration per real request."),
    Metric("tpot", "Time per Output Token", CAT_PERF, LIVE, "ms", False,
           "Average time to generate each output token.",
           "eval_duration / eval_count, averaged over recent real requests."),
    Metric("tps", "Tokens per Second", CAT_PERF, LIVE, "tok/s", True,
           "Generation throughput of the model.",
           "eval_count / eval_duration per request; rolling mean."),
    Metric("throughput", "Throughput (Requests/min)", CAT_PERF, LIVE, "req/min", True,
           "How many requests the system serves per minute.",
           "Count of completed inferences in the trailing 60 s window."),
    Metric("error_rate", "Error Rate", CAT_PERF, LIVE, "%", False,
           "Share of requests that fail, time out, or are refused.",
           "Failed inferences / total inferences in the telemetry window."),
    Metric("token_efficiency", "Token Efficiency", CAT_PERF, LIVE, "ratio", True,
           "Output produced per total token processed (prompt+gen).",
           "gen_tokens / (prompt_tokens + gen_tokens), rolling mean."),
    Metric("tail_latency", "Tail Latency (p95/p99)", CAT_PERF, LIVE, "ms", False,
           "Worst-case end-to-end response time.",
           "p95 and p99 of measured wall-clock latency over the window."),

    # ── 8-9  Cost ─────────────────────────────────────────────────────────────
    Metric("tco", "Total Cost of Ownership", CAT_COST, ESTIMATED, "$/1k req", False,
           "Full operational cost per unit of work.",
           "GPU power draw × local $/kWh, amortised over measured requests."),
    Metric("price", "Price per 1M tokens", CAT_COST, ESTIMATED, "$/1M tok", False,
           "Marginal inference cost.",
           "Energy per token from measured tok/s and GPU wattage (local model, no API price)."),

    # ── 10  Model Architecture ────────────────────────────────────────────────
    Metric("parameters", "Parameters", CAT_ARCH, ESTIMATED, "B", None,
           "Model size / complexity.",
           "Parsed from the Ollama model name and /api/show metadata."),

    # ── 11-14  Accuracy & Hallucination ───────────────────────────────────────
    Metric("hallucination", "Hallucination Rate", CAT_ACCURACY, NA, "%", False,
           "Rate of factually unsupported statements.",
           "", na_reason="Needs an independent judge (TruthfulQA/HHEM-style). A 7B model "
                         "grading its own factuality is circular; pluggable when an API judge exists.",
           confidence=LOW),
    Metric("toxicity", "Toxicity & Bias", CAT_ACCURACY, ON_DEMAND, "%", False,
           "Share of prompts eliciting toxic or biased output.",
           "Dedicated local classifier (detoxify) over provocation prompts; N/A if not installed.",
           suite="toxicity", confidence=HIGH),
    Metric("pii_leakage", "PII Leakage", CAT_ACCURACY, ON_DEMAND, "%", False,
           "Tendency to emit personal/sensitive data.",
           "Regex detectors (cards, IBAN, email, phone, SSN) over PII-baiting prompts.",
           suite="pii"),
    Metric("copyright", "Copyright Infringement", CAT_ACCURACY, NA, "%", False,
           "Verbatim reproduction of training material.",
           "", na_reason="Needs a licensed corpus + CopyrightCatcher/DE-COP tooling not available locally.",
           confidence=LOW),

    # ── 15-18  Semantic & Contextual ──────────────────────────────────────────
    Metric("semantic_sim", "Semantic Similarity", CAT_SEMANTIC, ON_DEMAND, "0-1", True,
           "Closeness of answers to reference responses.",
           "Cosine similarity of SUNI's own MiniLM embeddings (answer vs reference).",
           suite="semantic_sim"),
    Metric("grounding", "Grounding Score", CAT_SEMANTIC, NA, "%", True,
           "Faithfulness to source documents in RAG.",
           "", na_reason="RAG faithfulness graded by the generator is circular; needs an "
                         "independent judge (RAGAS/TruLens). Pluggable later.",
           confidence=LOW),
    Metric("prompt_sensitivity", "Prompt Sensitivity", CAT_SEMANTIC, ON_DEMAND, "0-1", False,
           "Output drift across rephrased-but-equivalent prompts.",
           "Embedding-variance of answers to paraphrase sets (lower = more stable).",
           suite="prompt_sensitivity"),
    Metric("model_variability", "Model Variability", CAT_SEMANTIC, ON_DEMAND, "0-1", False,
           "Output consistency across identical repeated runs.",
           "Embedding-variance of N runs of the same prompt at fixed temperature.",
           suite="variability"),

    # ── 19-20  Format & Instruction Following ─────────────────────────────────
    Metric("format_compliance", "Format Compliance", CAT_FORMAT, ON_DEMAND, "%", True,
           "Adherence to requested structured output.",
           "Parse the output (JSON/CSV/markdown) and check it validates + carries required keys.",
           suite="format"),
    Metric("instruction_following", "Instruction Following", CAT_FORMAT, ON_DEMAND, "%", True,
           "Compliance with explicit, checkable constraints.",
           "IFEval-style verifiable rules (word count, keywords, case, no-comma…) checked in code.",
           suite="ifeval"),

    # ── 21-24  Agent Behaviour ────────────────────────────────────────────────
    Metric("tool_accuracy", "Tool-Calling Accuracy", CAT_AGENT, ON_DEMAND, "%", True,
           "Correct tool + argument selection for a task.",
           "Prompts with a known-correct call from SUNI's real registry; compare emitted tool_calls.",
           suite="tool_calling"),
    Metric("subgoal_success", "Subgoal Success Rate", CAT_AGENT, ON_DEMAND, "%", True,
           "Success on individual steps of a multi-step task.",
           "Multi-step tasks with checkable per-step outcomes; fraction of subgoals met.",
           suite="tool_calling"),
    Metric("plan_stability", "Plan Stability", CAT_AGENT, LIVE, "changes/task", False,
           "How often the agent revises its plan mid-task.",
           "Task-mode plan revisions counted per task from orchestrator activity."),
    Metric("self_correction", "Self-Correction Score", CAT_AGENT, LIVE, "%", True,
           "Recovery after a failed tool call.",
           "Fraction of tool failures followed by a successful retry in the same task (from trace)."),

    # ── 25-26  Safety & Security ──────────────────────────────────────────────
    Metric("jailbreak", "Jailbreak Resistance", CAT_SAFETY, ON_DEMAND, "%", True,
           "Resilience to manipulation into disallowed output.",
           "Known attack prompts; success detected by string-matching the forbidden payload/refusal.",
           suite="jailbreak"),
    Metric("prompt_injection", "Prompt Injection Resistance", CAT_SAFETY, ON_DEMAND, "%", True,
           "Resistance to injected instructions in untrusted content.",
           "Canary hidden in doc/email content; pass = canary NOT emitted. Tests SUNI's UNTRUSTED markers.",
           suite="prompt_injection"),

    # ── 27  Context Understanding ─────────────────────────────────────────────
    Metric("ruler", "RULER (long-context retrieval)", CAT_CONTEXT, ON_DEMAND, "%", True,
           "Retrieval accuracy from long contexts.",
           "Needle-in-haystack at increasing depths within num_ctx; exact-match on the needle.",
           suite="ruler"),

    # ── 28-33  Standardised Benchmarks ────────────────────────────────────────
    Metric("gsm8k", "GSM8K", CAT_BENCH, ON_DEMAND, "%", True,
           "Multi-step grade-school math reasoning.",
           "Bundled subset; extract final number and exact-match the gold answer.",
           suite="gsm8k"),
    Metric("gpqa", "GPQA", CAT_BENCH, NA, "%", True,
           "Graduate-level science QA.",
           "", na_reason="Gated dataset; requesting access + full multiple-choice harness is out of scope "
                         "for a bundled local run. Add the dataset to enable.",
           confidence=LOW),
    Metric("mmlu_pro", "MMLU-Pro", CAT_BENCH, NA, "%", True,
           "Broad multi-domain knowledge (12k+ Qs).",
           "", na_reason="Full 12k-question set is too large to bundle and slow on a local 7B. "
                         "Add the dataset to enable a sampled run."),
    Metric("mbpp", "MBPP", CAT_BENCH, ON_DEMAND, "%", True,
           "Basic Python programming.",
           "Bundled subset; execute the model's function against the provided asserts in a sandbox.",
           suite="mbpp"),
    Metric("swe_bench", "SWE-Bench", CAT_BENCH, NA, "%", True,
           "Resolving real GitHub issues.",
           "", na_reason="Requires per-issue Docker repos + build/test harness; far beyond a local 7B "
                         "and this runner's scope."),
    Metric("lmsys_arena", "LMSYS Chatbot Arena", CAT_BENCH, NA, "Elo", True,
           "Human-preference model ranking.",
           "", na_reason="Inherently comparative human evaluation across many models; not reproducible "
                         "for a single local model."),
]

# id → Metric
_BY_ID = {m.id: m for m in METRICS}


def metric(metric_id: str) -> Metric | None:
    return _BY_ID.get(metric_id)


def by_category() -> list[dict]:
    """Metrics grouped in display order, for the dashboard."""
    groups: dict[str, list] = {c: [] for c in CATEGORY_ORDER}
    for m in METRICS:
        groups.setdefault(m.category, []).append(m)
    return [
        {"category": c, "metrics": [m.to_dict() for m in groups[c]]}
        for c in CATEGORY_ORDER if groups.get(c)
    ]


def suite_metric_ids(suite_key: str) -> list[str]:
    """All metric ids produced by a given suite (a suite may feed several)."""
    return [m.id for m in METRICS if m.suite == suite_key]


def all_suite_keys() -> list[str]:
    """Distinct on-demand suite keys, in metric order."""
    seen, out = set(), []
    for m in METRICS:
        if m.source == ON_DEMAND and m.suite and m.suite not in seen:
            seen.add(m.suite)
            out.append(m.suite)
    return out


# counts for logging / sanity
def status_summary() -> dict:
    from collections import Counter
    c = Counter(m.source for m in METRICS)
    return {"total": len(METRICS), **c}
