"""
Model tier system for SUNI.

Tiers
-----
  1  nano   1–4 B params   — fast lookups, simple Q&A, formatting
  2  core   5–14 B params  — default balanced model (e.g. qwen2.5:7b)
  3  large  15–44 B params — complex reasoning, multi-step analysis
  4  max    45 B+ params   — maximum local capability
  5  code   Claude Code    — top escalation; also used for coding/system tasks

Complexity scoring
------------------
Returns an integer 1–4 representing the suggested starting local tier.
The orchestrator caps this at MAX_LOCAL_TIER from system_profile.

Escalation detection
--------------------
needs_escalation(text) returns True when the model's response signals it
cannot produce a good answer — triggering a retry on the next tier.
"""
from __future__ import annotations
import re

# ── Tier metadata ─────────────────────────────────────────────────────────────

TIER_NAMES: dict[int, str] = {
    1: "nano",
    2: "core",
    3: "large",
    4: "max",
    5: "code",
}

# Parameter count ranges (billions) that map to each local tier
TIER_PARAM_RANGES: dict[int, tuple[int, int]] = {
    1: (1,  4),
    2: (5,  14),
    3: (15, 44),
    4: (45, 999),
}

CLAUDE_CODE_TIER = 5


def params_to_tier(params_b: float) -> int:
    """Return local tier (1–4) for a model with params_b billion parameters."""
    for tier, (lo, hi) in TIER_PARAM_RANGES.items():
        if lo <= params_b <= hi:
            return tier
    return 4 if params_b > 44 else 1


# ── Complexity scoring ────────────────────────────────────────────────────────

# Strong signals that a task needs at least Tier 3
_COMPLEX_RE = re.compile(
    r'\b('
    r'analyse|analyze|compare|contrast|evaluate|critique|synthesise|synthesize|'
    r'explain\s+why|explain\s+how|walk\s+me\s+through|step\s+by\s+step|'
    r'write\s+a\s+(detailed|comprehensive|full|complete|long)|'
    r'pros\s+and\s+cons|trade.?off|recommend(ation)?|strategy|'
    r'research|investigate|summarise|summarize\s+.{20,}|'
    r'translate\s+.{30,}|rewrite\s+.{30,}'
    r')\b',
    re.IGNORECASE,
)

# Signals a simple lookup / tier-1 task
_SIMPLE_RE = re.compile(
    r'^('
    r'what\s+is|what\'s|who\s+is|when\s+is|where\s+is|'
    r'define\s+|spell\s+|convert\s+|calculate\s+|how\s+many|how\s+much|'
    r'list\s+\w+|give\s+me\s+a\s+list|name\s+\d+'
    r')\b',
    re.IGNORECASE,
)

# Signals multi-step work — favour Tier 2 minimum
_MULTI_STEP_RE = re.compile(
    r'\b(then|after\s+that|also|additionally|furthermore|'
    r'first.*then|step\s+\d|finally)\b',
    re.IGNORECASE,
)


def complexity_score(text: str) -> int:
    """
    Return suggested starting tier (1–4) based on the user message.

    The orchestrator will further cap this at system_profile.MAX_LOCAL_TIER.
    """
    words = len(text.split())

    # Very long messages are inherently more complex
    if words > 200:
        base = 3
    elif words > 80:
        base = 2
    else:
        base = 1

    # Boost for complex-task keywords
    if _COMPLEX_RE.search(text):
        base = max(base, 3)

    # Boost for multi-step signals
    if _MULTI_STEP_RE.search(text):
        base = max(base, 2)

    # Downgrade clear simple lookups
    if base == 1 and _SIMPLE_RE.match(text.strip()):
        return 1

    return min(base, 4)


# ── Escalation detection ──────────────────────────────────────────────────────

_ESCALATION_RE = re.compile(
    r'\b('
    r"i('m|\s+am)\s+(not\s+sure|uncertain|unable|sorry)|"
    r'i\s+(cannot|can\'t|don\'t|do\s+not)\s+(access|browse|fetch|retrieve|connect|process)|'
    r'(beyond|outside)\s+(my\s+)?(capabilities|knowledge|scope|training)|'
    r'i\s+lack\s+(the\s+)?(ability|access|knowledge)|'
    r'(my|this)\s+(training|knowledge)\s+(data\s+)?(doesn\'t|does\s+not|only\s+goes)|'
    r'as\s+an\s+ai\s+(language\s+model\s+)?i\s+(cannot|can\'t)|'
    r'i\s+don\'t\s+have\s+(real.?time|live|current|up.to.date)'
    r')\b',
    re.IGNORECASE,
)

# These phrases suggest the model is hedging heavily — worth escalating
_HEDGE_RE = re.compile(
    r'\b(i\'m\s+not\s+entirely\s+(sure|certain)|'
    r'you\s+(may\s+want\s+to\s+consult|should\s+check\s+with|might\s+want\s+to\s+verify)|'
    r'please\s+(verify|double.check|consult\s+a\s+professional))\b',
    re.IGNORECASE,
)


def needs_escalation(response_text: str) -> bool:
    """
    Return True if the model's response signals it cannot handle the task.
    Used by the orchestrator to decide whether to retry on a higher tier.
    """
    if _ESCALATION_RE.search(response_text):
        return True
    # Only escalate on hedging if the response is very short (likely a refusal)
    if len(response_text.split()) < 60 and _HEDGE_RE.search(response_text):
        return True
    return False


# ── Direct Claude Code routing ────────────────────────────────────────────────

# Self-contained code/system work — strong signals for T5 direct routing
_DIRECT_CC_RE = re.compile(
    r'('
    # Code writing with explicit language name or artifact type
    r'\b(write|create|implement|build|generate)\s+(a\s+)?(python|javascript|typescript|'
    r'powershell|bash|shell|js|ts|c\#|c\+\+|rust|go|java|sql|html|css)\b|'
    r'\b(write|create|implement|build|generate)\s+(a\s+)?(script|function|class|module|'
    r'program|cli\s+tool|api\s+client|migration|test\s+suite|unit\s+tests?)\b|'
    # Code writing with language anywhere in the sentence
    r'\b(write|create|implement|build|generate)\b.{0,60}\b(python|javascript|typescript|'
    r'powershell|bash|shell|rust|go|java|sql)\b|'
    # Code modification (requires code file context)
    r'\b(refactor|debug|rewrite)\s+.{0,50}(code|function|class|module|script|method)\b|'
    r'\bfix\s+the\s+(bug|error|exception|traceback)\b|'
    r'\b(add|remove|update|rename)\b.{0,30}\b(method|function|class|endpoint|route|'
    r'field|column|migration|test|fixture)\b|'
    # File extensions — working with code or config files
    r'\.(py|js|ts|jsx|tsx|cs|rs|go|java|cpp|c|h|php|rb|sh|ps1|psm1|'
    r'dockerfile|docker-compose\.yml|\.env\.example)\b|'
    # Git operations
    r'\bgit\s+(commit|push|pull|merge|rebase|branch|checkout|stash|diff|log|blame|'
    r'cherry.pick|reset|revert|tag)\b|'
    # System/infrastructure tasks
    r'\b(scheduled?\s+task|task\s+scheduler|schtasks|cron\s+job|systemd\s+service)\b|'
    r'\b(docker\s+(build|run|compose|push|pull)|npm\s+(install|run|build|publish)|'
    r'pip\s+install\s+\S|pipenv|poetry\s+(add|install|run))\b|'
    r'\b(deploy\s+(the|to|on)|compile\s+the|containeris[ez]|'
    r'set\s+up\s+(a\s+)?(ci|cd|github\s+actions?|gitlab\s+ci|workflow|pipeline))\b|'
    r'\b(initialise|initialize)\s+(a\s+)?(git\s+repo|fastapi|flask|django|react|'
    r'vue|svelte|express|next\.?js)\b|'
    # Codebase/architecture analysis
    r'\b(audit|analyse|analyze)\s+(the\s+)?(code(base)?|repo(sitory)?|project|'
    r'architecture|dependencies|imports?)\b|'
    r'\bfind\s+(all|every|any)\s+(bugs?|errors?|references?|usages?|imports?|calls?|'
    r'occurrences?|duplicates?|dead\s+code)\b|'
    # Project/doc generation
    r'\b(create|update|write)\s+(the\s+)?(claude\.md|readme\.md|changelog|'
    r'migration\s+guide|api\s+docs?|openapi\s+spec)\b'
    r')',
    re.IGNORECASE,
)

# SUNI proprietary tool tasks — must stay local so SUNI tools are available
_SUNI_TOOLS_RE = re.compile(
    r'\b(send\s+(an?\s+)?email|email\s+(this|me|him|her|them)|'
    r'check\s+(my\s+)?email|(my\s+)?inbox|reply\s+to|'
    r'knowledge\s+base|company\s+(doc|policy|procedure|manual)|'
    r'hr\s+(doc|policy|form|procedure)|'
    r'search\s+(the\s+)?(kb|knowledge|company\s+doc|our\s+doc)|'
    r'article|suniverse|our\s+(internal|company)\s+(docs?|files?)|'
    r'remember\s+(that|this|when|my)|forget\s+(that|this)|'
    r'what\s+do\s+you\s+(know|remember)\s+about\s+me|user\s+preference|'
    # URL + PDF/summary tasks — handled by web_fetch + create_pdf, not Claude Code
    r'(pdf|summary|summarise|summarize|document).{0,100}https?://|'
    r'https?://.{0,100}(pdf|summary|summarise|summarize|document))\b',
    re.IGNORECASE,
)


def direct_to_claude_code(text: str) -> bool:
    """
    Return True if the task should be routed directly to Claude Code (T5),
    bypassing all local model tiers.

    Only matches self-contained code/system work — tasks that need none of
    SUNI's proprietary tools (KB, email, articles, memory). Conservative by
    design: when in doubt return False; the local model can still delegate
    via the claude_task tool if needed.
    """
    if _SUNI_TOOLS_RE.search(text):
        return False  # needs SUNI's tool orbit — keep local
    return bool(_DIRECT_CC_RE.search(text))
