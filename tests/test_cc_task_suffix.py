"""The Claude Code task suffix must not invite the CLI to narrate SUNI's plumbing.

SUNI answered a question about a company it had no documents for with "Nada nos
seus documentos indexados — procurei em doc_meta.json, doc_scan.json e na
memória". The KB hint hands the CLI `memory/doc_meta.json` and tells it to grep,
so the CLI reported its search honestly — internal layout in a reply meant to
say "nothing found".

WHAT THESE TESTS ARE: deletion guards. They prove the instruction is present,
correctly placed, and not reachable-only-via-config. They CANNOT prove a model
obeys it — that takes a real KB-miss question against the live CLI. Do not read a
pass here as "the leak is fixed".
"""
from __future__ import annotations

from suni.models.claude_code_agent import _task_suffix, _cc_persona

# Not a path under C:\Users: the release gate rejects that shape on sight, and it
# is right to — it cannot tell a placeholder name from a real one.
OUT = "D:/SUNI/output"


def _reporting_block(s: str) -> str:
    """Just the [Reporting rule: ...] block."""
    start = s.index("[Reporting rule:")
    return s[start:]


def test_the_reporting_rule_is_present():
    s = _task_suffix(OUT)
    assert "[Reporting rule:" in s
    block = _reporting_block(s)
    # The two halves that matter: report findings, not the search.
    assert "never" in block.lower() and "where you looked" in block.lower()
    assert "narrate" in block.lower()


def test_the_functional_hints_still_work():
    """The fix must not cost the CLI its output dir or its KB entry point."""
    s = _task_suffix(OUT)
    assert OUT in s                          # where to save generated files
    assert "memory/doc_meta.json" in s       # how to find indexed documents
    assert "do not ask the user to map a drive" in s


def test_the_rule_comes_after_the_plumbing_it_governs():
    s = _task_suffix(OUT)
    assert s.index("[Reporting rule:") > s.index("[Knowledge Base:") > s.index("[File rule:")


def test_the_rule_does_not_recite_filenames():
    """A denylist of names would be both leaky and incomplete.

    The reply that prompted this named `doc_scan.json`, which the suffix never
    mentions — the CLI found it by listing `memory/`. So the rule has to forbid
    the behaviour, not enumerate today's filenames; and listing names inside a
    prohibition invites echoing them back.
    """
    block = _reporting_block(_task_suffix(OUT))
    assert ".json" not in block
    assert "doc_scan" not in block
    assert "doc_meta" not in block


def test_the_rule_does_not_live_in_the_overridable_persona(monkeypatch):
    """It must be code, not persona.

    `claude_code_persona` is a config value, so any install that sets its own
    persona would silently lose a persona-based fix — which is the case on the
    box where this leaked.
    """
    from suni import config as suni_config
    monkeypatch.setattr(
        suni_config, "get",
        lambda k, d=None: "You are SUNI. Dry wit." if k == "claude_code_persona" else d,
    )
    assert "Reporting rule" not in _cc_persona()      # not where it's enforced
    assert "[Reporting rule:" in _task_suffix(OUT)    # still applied anyway
