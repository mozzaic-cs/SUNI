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
    "connecting to postgres://admin:hunter2@127.0.0.1:5432/x",
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
             "postgres://admin:hunter2@127.0.0.1/x"]
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
    ("postgres://admin:hunter2@127.0.0.1/x", "hunter2"),
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


# ── she has to look before she answers ──────────────────────────────────────
@pytest.mark.parametrize("text", [
    "what's wrong with you?",
    "Está tudo bem contigo?",
    "estás bem contigo?",
    "tens algum problema?",
    "o que se passa contigo?",
    "are you ok?",
    "why are you so slow?",
    "não estás a responder",
    "estás muito lenta hoje",
    "is everything working?",
    "diagnose yourself",
])
def test_asking_how_she_is_triggers_a_real_look(text):
    """diagnose_self was registered and unreachable: an admin turn goes
    straight to the Claude Code CLI, which brings its own tools and never sees
    SUNI's registry. Asked "what's wrong with you?" she answered from
    imagination while three turns hung and a model thrashed. The tool was not
    declined — it was never offered. So the intent is recognised before
    routing, which works whichever model ends up answering.
    """
    from suni.core.orchestrator import _SELFCHECK_RE
    assert _SELFCHECK_RE.search(text), f"{text!r} does not ask her to look"


@pytest.mark.parametrize("text", [
    "está tudo bem, obrigado",                      # a statement, not a question
    "tudo bem com o cliente?",                      # about somebody else
    "o que se passa com o projeto?",
    "how are you going to do that?",                # "how are you" but not about her
    "diagnostica o problema do servidor do cliente",
    "faz um diagnóstico à rede do cliente",
    "a impressora está lenta",
    "bom dia",
])
def test_it_does_not_fire_on_other_peoples_problems(text):
    """"Diagnose" is a word about other people's servers too. Reading the log
    out because somebody asked after a client's printer would be worse than
    doing nothing."""
    from suni.core.orchestrator import _SELFCHECK_RE
    assert not _SELFCHECK_RE.search(text), f"{text!r} wrongly triggers a self-check"


def test_a_channel_user_asking_how_she_is_gets_no_log_report():
    """The findings quote log lines. An inbound channel runs at "standard"
    because it is not per-sender authenticated, and a pleasantry from Telegram
    must not return the contents of the machine's log."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "suni/core/orchestrator.py").read_text(encoding="utf-8")
    fn = src[src.index("async def _maybe_self_check"):]
    fn = fn[:fn.index("async def _maybe_show_network")]
    assert 'user_role not in ("admin", "owner")' in fn, "any role can read the logs"
    assert fn.index("user_role not in") < fn.index("_diag.run()"), (
        "the check runs before the role is tested")


def test_a_clean_bill_of_health_is_not_recited():
    """The right answer to "are you all right?" is "yes" — not a log report.
    She needs the findings to know which, and instructions not to perform them."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "suni/core/orchestrator.py").read_text(encoding="utf-8")
    fn = src[src.index("async def _maybe_self_check"):]
    fn = fn[:fn.index("async def _maybe_show_network")]
    assert "without reciting" in fn
    assert "do not add causes of your" in fn, "she may invent a cause beside the evidence"


# ── per-user settings, read per user ────────────────────────────────────────
def test_no_user_facing_setting_is_read_from_the_instance_default():
    """Made this mistake twice in one session, in two files.

    The voice path overrode a chosen female voice with the local male one, and
    the speech path told whisper that Portuguese was English — both by reading
    the INSTANCE config where a per-user setting exists. The instance default
    is en-GB; this user is pt-PT. So the sweep, as a test.

    A global read is legitimate only as a LAST RESORT, after the caller's own
    value: `language or _cfg.get(...)`. What is not legitimate is reading the
    global when a user is in scope and never asking them.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "suni"
    per_user = ["stt_language", "response_language", "tts_voice",
                "allowed_mcp_servers", "output_dir", "notify_to"]
    pat = re.compile(r'(?:config|_cfg|suni_config|cfg)\.get\(\s*["\'](' +
                     "|".join(per_user) + r')["\']')
    offenders = []
    for f in root.rglob("*.py"):
        if f.name == "user_settings.py":
            continue
        raw = f.read_text(encoding="utf-8-sig").splitlines()
        # Join continuation lines before looking. A line-by-line scan reported
        # `or _cfg.get("stt_language", "en-GB")` as an offender when it is the
        # SECOND half of `response_language or _cfg.get(...)` — the correct
        # shape, split across two lines.
        lines, buf, start, depth = [], "", 1, 0
        for n, ln in enumerate(raw, 1):
            st = ln.strip()
            if not buf:
                start = n
            buf = (buf + " " + st).strip() if buf else st
            # Bracket depth, which is what actually says whether a statement has
            # finished. Guessing from leading/trailing keywords glued the tail
            # of one expression onto the head of the next.
            depth += sum(st.count(c) for c in "([{") - sum(st.count(c) for c in ")]}")
            if depth > 0 or st.endswith("\\"):
                continue
            depth = 0
            lines.append((start, buf))
            buf = ""
        if buf:
            lines.append((start, buf))
        for n, line in lines:
            m = pat.search(line)
            if not m:
                continue
            # A fallback AFTER the caller's own value is the correct shape.
            if re.search(r'\b(language|response_language|voice)\s+or\s', line):
                continue
            # Describing the global config to the admin panel is not resolving
            # a user's preference.
            if "has_" in line or 'cfg["' in line:
                continue
            offenders.append(f"{f.relative_to(root.parent)}:{n}: {line.strip()[:90]}")
    assert not offenders, (
        "a per-user setting is being read from the instance config:\n  "
        + "\n  ".join(offenders))


def test_meeting_transcription_uses_the_speakers_language():
    """The place the same bug was waiting: transcribe_file(wav) with no
    language falls through to the instance default, so a Portuguese meeting
    would have come back as English."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "suni/tools/meeting_tool.py").read_text(encoding="utf-8")
    assert "_tx.transcribe_file(wav, language=" in src, (
        "the meeting is transcribed without saying what language it is in")
    assert "user_settings" in src, "the language does not come from the speaker"
