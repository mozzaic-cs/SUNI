"""
Regression cases for the release scan's detectors.

`scripts/prepare_public_release.py` is the control the release runbook rests on:
nothing is published until it prints SECRET SCAN CLEAN. It has gone blind twice,
both times silently, and both times while tuning the patterns to reduce noise:

  · a bare identifier treated as "a variable, not a literal" — but a real
    password like hunter2xyz is indistinguishable from an identifier;
  · `\\b` before a credential keyword — `\\b` never matches after an underscore,
    so SUNI_SMTP_PASS slipped through.

A detector that stops detecting produces exactly the same output as a clean
repo, so the only way to notice is to assert that known-bad strings are still
caught. Each pattern list gets both directions: things that MUST flag, and
things that must NOT (because a noisy gate is one people learn to ignore, which
is how it went blind in the first place).

Unlike tests/test_separation.py — which is untracked, since it must contain the
strings it hunts for — this file ships, so a clone gets the same protection.
"""
from __future__ import annotations
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "prepare_public_release.py"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(),
                                reason="release script not present in this checkout")


@pytest.fixture(scope="module")
def scan():
    """Import the script without running it (it reads sys.argv at main())."""
    spec = importlib.util.spec_from_file_location("_release_scan", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, ["prepare_public_release.py"]
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


def _matches(patterns, text):
    """The `why` labels of every pattern that fires on `text`."""
    return [why for pat, why in patterns if re.search(pat, text, re.IGNORECASE)]


# ── addresses ────────────────────────────────────────────────────────────────
# Added 2026-08-25 after a test written from a live session carried a real
# address in its docstring AND an assertion. The host-specific pattern that
# existed happened to catch it; an address at any other domain would not have
# been seen at all.

@pytest.mark.parametrize("line", [
    # Fabricated, deliberately. The address that prompted this was real, and
    # writing it here to prove it gets caught would publish it in the file that
    # catches it — this test is tracked, unlike tests/test_separation.py.
    'to="j.doe@gmail.com"',
    'contact = "alice.smith@acme.co.uk"',
    '# ping bob@internal-corp.io if this breaks',
    'FROM_ADDR = "no-reply@somecompany.de"',
])
def test_a_real_address_is_caught_whatever_the_domain(scan, line):
    assert _matches(scan.PERSONAL_PATTERNS, line), f"not detected: {line}"


@pytest.mark.parametrize("line", [
    'to="maria.silva@example.pt"',        # RFC 2606, in a Portuguese fixture
    'url = "https://hunter2@logs.example"',
    'smtp = "user@mail.local"',           # RFC 6761
    'addr = "someone@host.invalid"',
    'x = "test@example.com"',
])
def test_reserved_domains_are_not_flagged(scan, line):
    """Tests need somewhere to point. Flagging the correct thing to write is
    how a gate earns the suppression that later blinds it."""
    assert "email address" not in _matches(scan.PERSONAL_PATTERNS, line), \
        f"false positive on a reserved domain: {line}"


# ── the two documented blind spots ───────────────────────────────────────────
@pytest.mark.parametrize("line", [
    'SUNI_SMTP_PASS = "hunter2xyz"',      # underscore prefix — the \b bug
    'password = "hunter2xyz"',            # bare identifier-shaped value
    'PWD=hunter2xyz;Server=x',            # connection string
    'postgres://admin:hunter2xyz@db:5432/x',
])
def test_the_credential_patterns_still_fire(scan, line):
    found = False
    for pat, _why in scan.CRED_ASSIGN_PATTERNS:
        for m in re.finditer(pat, line):
            if not scan._looks_placeholder(m.group(1)):
                found = True
    assert found, f"credential not detected: {line}"


@pytest.mark.parametrize("line", [
    'password = get_password()',          # call syntax, not a literal
    'password = "your-password-here"',    # placeholder
    'password = "<REDACTED>"',
])
def test_the_credential_patterns_stay_quiet_on_non_secrets(scan, line):
    for pat, _why in scan.CRED_ASSIGN_PATTERNS:
        for m in re.finditer(pat, line):
            assert scan._looks_placeholder(m.group(1)), f"false positive: {line}"


# ── coverage, not just detection ─────────────────────────────────────────────
def test_the_seed_includes_the_places_leaks_actually_appear(scan):
    """A perfect detector pointed at the wrong files finds nothing. `scripts/`
    was outside the seed once, and an SMTP password sat there unscanned."""
    files = set(scan.collect_files())
    assert any(f.startswith("tests/") for f in files), "tests/ is not scanned"
    assert any(f.startswith("suni/") for f in files)
    assert "README.md" in files


# Deliberately NOT here: a "the seed is clean today" test. The first draft of
# this file had one, and it flagged four things that belong in a published repo
# — MOZZAIC in pyproject and docs/comparison.html (authorship), and the
# C:/Users/yourusername placeholder that pdf_tool.py documents on purpose. The
# real scan applies suppressions this file would have to reimplement, and a
# reimplementation drifts from what actually gates the release. A duplicate that
# cries wolf is worse than no duplicate: it is the noise that gets checks
# switched off, which is the documented cause of both earlier blindings.
#
# That check belongs in one place — scripts/prepare_public_release.py — and it
# is a release step, not a per-commit one. What this file protects is that the
# detectors behind it still detect.
