"""
On-demand benchmark suites — the *capability* half of the system.

Each suite is an async callable ``run(gen, ctx) -> SuiteResult`` where:

  gen   an async generator supplied by the runner (runner.py). Contract:
          await gen(prompt, *, system=None, temperature=0.0, seed=0,
                    num_predict=512, tools=None, format=None) -> dict
        returning {"text": str, "tool_calls": list[dict], "error": str|None}.
        The runner pins temperature/seed so runs are reproducible.

  ctx   a dict of shared context: {"limit": int|None, "progress": callable|None,
        "num_ctx": int, "registry_tools": list[dict]}.

A suite returns a SuiteResult carrying one or more metric ids (see metrics.py
`suite=` fields). Every scorer here is OBJECTIVE — numeric match, test
execution, regex, parse, or attack-success detection. No model judges itself.

Datasets are small, bundled, and clearly indicative — the dashboard labels
scores "indicative subset, not official benchmark figures".
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict


@dataclass
class SuiteResult:
    suite: str
    metrics: dict                       # metric_id -> value (number or None)
    n: int = 0                          # items evaluated
    passed: int = 0
    details: list = field(default_factory=list)   # small per-item records
    confidence: str = "high"
    notes: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        # keep the payload small — cap per-item detail
        d["details"] = d["details"][:50]
        return d


# ── shared embedding helper (MiniLM, CPU) for semantic suites ─────────────────
_MODEL = None


def _embedder():
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer("all-MiniLM-L6-v2", device="cpu")
    return _MODEL


def embed(texts: list[str]):
    import numpy as np
    vecs = _embedder().encode(texts, batch_size=16, normalize_embeddings=True)
    return np.asarray(vecs, dtype="float32")


def cosine(a, b) -> float:
    import numpy as np
    return float(np.dot(a, b))   # inputs already normalised


# ── suite registry ────────────────────────────────────────────────────────────
# suite_key -> async run(gen, ctx). Populated by importing the modules below.
SUITES: dict = {}


def register(key: str, fn) -> None:
    SUITES[key] = fn


def get(key: str):
    return SUITES.get(key)


# Import suite modules so they self-register. Kept at the bottom to avoid
# circular imports (modules import helpers from this package).
from . import reasoning      # noqa: E402,F401  gsm8k
from . import coding         # noqa: E402,F401  mbpp
from . import instructions   # noqa: E402,F401  ifeval, format
from . import agentic        # noqa: E402,F401  tool_calling (+ subgoal)
from . import safety         # noqa: E402,F401  jailbreak, prompt_injection, pii
from . import context        # noqa: E402,F401  ruler
from . import semantic       # noqa: E402,F401  semantic_sim, prompt_sensitivity, variability
from . import toxicity       # noqa: E402,F401  toxicity (conditional)
