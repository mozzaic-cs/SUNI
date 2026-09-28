"""She reads her own logs and says what is wrong.

From the afternoon a Telegram message was received, logged, routed and never
answered. The cause was three facts in two log files, and the only way they
were found was a person reading logs over the owner's shoulder.
"""
from __future__ import annotations

import pathlib

import pytest

from suni import diagnostics as D

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = (ROOT / "suni/web/server.py").read_text(encoding="utf-8-sig")


# ── the check that would have answered that afternoon ───────────────────────
def test_a_turn_that_never_finished_is_reported():
    lines = [
        "2026-09-28 14:42:28 | INFO | [REQUEST] 'hello' ctx~7 tok role=standard",
        "2026-09-28 14:42:29 | INFO | [ROUTE]   local",
        # and then nothing — which is exactly what it looks like from outside
    ]
    fs = D._check_turns(lines)
    assert any(f.level == D.BROKEN for f in fs), (
        "a turn with no completion is the whole symptom and went unreported")


def test_turns_that_did_finish_are_not_reported_as_stuck():
    lines = [
        "… | [REQUEST] 'hello'",
        "… | [DONE]    total=7.56s route=local tools=[]",
    ]
    fs = D._check_turns(lines)
    assert not any(f.level == D.BROKEN for f in fs)
    assert any(f.level == D.OK for f in fs), "a clean result must say so"


def test_a_slow_turn_is_called_slow_not_broken():
    lines = ["… | [REQUEST] 'x'", "… | [DONE] total=97.79s route=local tools=[]"]
    fs = D._check_turns(lines)
    assert any(f.level == D.SLOW for f in fs)
    assert not any(f.level == D.BROKEN for f in fs)


# ── it must never hand back a credential ────────────────────────────────────
@pytest.mark.parametrize("secret_line", [
    "connecting to postgres://admin:hunter2@db.internal:5432/x",
    "Authorization: Bearer 8869766917:AAEWezqZblWaljUDH0NyR1KNB6r4ok2fe",
    "telegram_bot_token=8869766917:AAEWezqZblWaljUDH0NyR1K",
    "api_key: sk-proj-abcdefghijklmnop",
])
def test_no_evidence_line_carries_a_credential(secret_line):
    """Logs hold connection strings, and libraries embed credentials in their
    error text. A bot token reached this repo inside a screenshot the same day
    this was written — the report must not be another way out."""
    cleaned = D._scrub(secret_line)
    for leak in ("hunter2", "AAEWezqZblWaljUDH0NyR1K", "sk-proj-abcdefghijklmnop"):
        assert leak not in cleaned, f"{leak!r} survived scrubbing: {cleaned!r}"


def test_evidence_from_a_log_line_is_scrubbed_before_it_leaves():
    lines = ["2026-09-28 10:00:00 | ERROR | connect failed for "
             "postgres://admin:hunter2@db/x"]
    fs = D._check_errors(lines)
    assert fs and "hunter2" not in fs[0].evidence


# ── a check that looked at nothing must not report health ───────────────────
def test_the_report_says_what_it_looked_at():
    """A list of problems cannot be told apart from a failure to look for them.

    The first version of the backend check read 400 KB of a 31 MB log — a few
    minutes of a chatty file — found no evictions, and pronounced the backend
    healthy minutes after one. It was looking at nothing and saying nothing was
    there.
    """
    result = D.run()
    assert result["checked"], "the report does not say what it examined"
    text = D.as_report(result)
    assert "I looked at:" in text
    assert len(result["checked"]) >= 4


def test_the_backend_log_is_read_in_a_window_wide_enough_to_contain_something():
    src = (ROOT / "suni/diagnostics.py").read_text(encoding="utf-8")
    i = src.index("def _check_backend")
    block = src[i:i + 1600]
    assert "window=" in block, "the backend check reads the default tail"
    win = int(block.split("window=")[1].split(")")[0].replace("_", ""))
    assert win >= 2_000_000, (
        f"{win} bytes of a 30 MB chatty log covers minutes, not the incident")


# ── it reads, and only reads ────────────────────────────────────────────────
def test_the_diagnosis_changes_nothing():
    """A diagnosis that restarts the thing it is diagnosing destroys its own
    evidence, and anything that writes is a second failure mode to debug."""
    import ast

    src = (ROOT / "suni/diagnostics.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Docstrings dropped first. The prose here says the word "restart" while
    # explaining that it never restarts anything, and a substring search over
    # the file failed on its own explanation — the same trap a scan test in
    # this suite fell into once before, on the word "pings".
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            node.value.value = ""
    code = ast.unparse(tree)
    for forbidden in ("config.save", "subprocess", "os.remove", "os.unlink",
                      "shutil", "Popen", "rmtree"):
        assert forbidden not in code, f"the diagnosis does more than read: {forbidden}"
    # Opened for writing anywhere would be a write.
    assert '"w"' not in code and "'w'" not in code, "something is opened for writing"


# ── who may ask ─────────────────────────────────────────────────────────────
def test_a_channel_user_cannot_read_the_machines_logs():
    from suni.tools import diagnose_tool
    from suni.tools.agent_tool import CURRENT_ROLE

    token = CURRENT_ROLE.set("standard")
    try:
        out = diagnose_tool.handler()
    finally:
        CURRENT_ROLE.reset(token)
    assert "administrator" in out.lower()
    assert "[BROKEN]" not in out and "evidence:" not in out, (
        "the refusal still leaked the report")


def test_an_admin_gets_the_report():
    from suni.tools import diagnose_tool
    from suni.tools.agent_tool import CURRENT_ROLE

    token = CURRENT_ROLE.set("admin")
    try:
        out = diagnose_tool.handler()
    finally:
        CURRENT_ROLE.reset(token)
    assert "I looked at:" in out


def test_it_is_registered_where_the_web_interface_can_reach_it():
    assert "diagnose_tool.SCHEMA" in SERVER, "the server registry never sees it"
    assert "diagnose_tool" in SERVER


def test_the_model_is_told_not_to_invent_a_cause():
    """The findings are measured. A guess in the same paragraph, in the same
    voice, is worse than no paragraph."""
    from suni.tools import diagnose_tool
    from suni.tools.agent_tool import CURRENT_ROLE

    token = CURRENT_ROLE.set("admin")
    try:
        out = diagnose_tool.handler()
    finally:
        CURRENT_ROLE.reset(token)
    assert "do not add causes of your own" in out


# ── the scrubber this relies on, probed with the real shapes ────────────────
@pytest.mark.parametrize("line,leak", [
    ("Authorization: Bearer 8869766917:AAEWezqZblWaljUDH0NyR1K", "AAEWezqZ"),
    ("telegram_bot_token=8869766917:AAEWezqZblWaljUDH0NyR1K", "AAEWezqZ"),
    ("the token is 8869766917:AAEWezqZblWaljUDH0NyR1KNB6r4ok2fe", "AAEWezqZ"),
    ("api_key: sk-proj-abcdefghijklmnopqrs", "sk-proj-abcdef"),
    ("X-Api-Key: abc123def456ghi789", "abc123def456"),
    ("password=hunter2", "hunter2"),
    ("postgres://admin:hunter2@db/x", "hunter2"),
])
def test_log_shipping_scrubber_holds_against_real_shapes(line, leak):
    """These are the shapes that got through, not invented ones.

    "Authorization: Bearer <token>" kept the token, because only the word
    "Bearer" was consumed. "telegram_bot_token=" never matched at all —
    \btoken\b finds no word boundary after an underscore, so the one key name
    SUNI actually stores a bot token under went to the remote collector intact.
    This scrubber is what log shipping uses, so these were not hypothetical.
    """
    from suni.log_ship import _safe
    assert leak not in _safe(line), f"{leak!r} survived: {_safe(line)!r}"


@pytest.mark.parametrize("ordinary", [
    "[DONE] total=7.56s route=local tools=[]",
    "load_tensors: offloaded 29/29 layers to GPU",
    "num_ctx=8192",
    "[TIER] start=2 max_local=3 (floor=core)",
])
def test_the_scrubber_does_not_destroy_ordinary_log_lines(ordinary):
    """A scrubber that redacts everything is safe and useless — and these are
    exactly the lines a diagnosis needs to quote."""
    from suni.log_ship import _safe
    assert _safe(ordinary) == ordinary
