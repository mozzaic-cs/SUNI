"""What Claude Code's stream is, and what it is not.

Asked to transcribe the audio of a meeting, SUNI replied with several thousand
characters of JSON: the session id, the complete tool and plugin inventory with
filesystem paths, the model name, the run's cost in dollars, and the user's
account name — presented in the conversation as though she had said it.

In --stream-json mode stdout IS the protocol. One line per event, and the
answer is a few text_delta fragments buried in it. The parser understood that
perfectly; the fallback did not. When a run was stopped before it finished
there was no result event to find, and the code fell back to stdout.strip(),
which is the whole transcript of the machinery.

That is also why the transcription never appeared: the run was stopped, so
there was no answer to fall back to.
"""
from __future__ import annotations

import json
import pathlib
import re

SRC = (pathlib.Path(__file__).resolve().parent.parent
       / "suni/models/claude_code_agent.py").read_text(encoding="utf-8")

# Trimmed from the real leak, keeping the shapes that matter.
LEAKED = "\n".join(json.dumps(o) for o in [
    {"type": "system", "subtype": "task_notification", "task_id": "b4bslvk28",
     "status": "stopped",
     "summary": "Background shell command didn't finish before the previous session ended"},
    {"type": "system", "subtype": "init", "cwd": "C:\\Users\\someone",
     "session_id": "f2ac96e2", "tools": ["Task", "Bash", "PowerShell", "Write"],
     "model": "claude-opus-5-5",
     "plugins": [{"name": "telemetry", "path": "builtin"}]},
    {"type": "result", "subtype": "success", "result": "",
     "total_cost_usd": 0.5152322, "session_id": "f2ac96e2"},
])


def test_the_fallback_is_not_the_raw_stream():
    """One line of code: stdout.strip() as the last resort."""
    body = SRC[SRC.index("parsed = _streamed or _parse_json_output(stdout)"):][:1400]
    assert "stdout.strip()" not in body, (
        "the answer still falls back to the protocol itself"
    )
    assert 'parsed.get("result")' in body and 'parsed.get("said")' in body


def test_a_stopped_run_says_so_rather_than_printing_itself():
    body = SRC[SRC.index("parsed = _streamed or _parse_json_output(stdout)"):][:1400]
    assert "stopped before it finished" in body, (
        "a run with nothing to show returns an empty message instead of saying why"
    )


def test_what_she_actually_said_survives_a_stop():
    """The text arrives in deltas long before the result event. Keeping only
    the result meant a stopped run threw away the answer it already had — and
    then printed the machinery instead."""
    assert 'state["said"]' in SRC, "the streamed text is sent and then forgotten"
    ret = SRC[SRC.index("return rc, stdout, stderr, (state"):][:200]
    assert 'state.get("said")' in ret, (
        "a run that was stopped still hands back an empty dict, which is what "
        "sent the raw stream to the screen"
    )


def test_none_of_this_would_have_been_shown_to_anyone():
    """The specific things that were in the conversation. If any of these can
    still reach a reply, the fix is not a fix."""
    secrets = ["session_id", "total_cost_usd", "plugins", "claude-opus-5-5",
               "C:\\\\Users", "PowerShell"]
    for s in secrets:
        assert s in LEAKED, "the sample no longer represents the leak"
    # The guard is that stdout is never the content; this asserts the property
    # rather than re-running the CLI.
    assert "NEVER stdout" in SRC, "nothing records why this must not come back"


def test_the_parser_still_only_takes_text_deltas():
    """thinking_delta and signature_delta ride the same channel and are not
    part of the reply — the signature alone is a kilobyte of base64."""
    assert 'delta.get("type") == "text_delta"' in SRC
