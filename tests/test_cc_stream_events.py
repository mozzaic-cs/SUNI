"""Claude Code stream mode: narration is progress, only the answer is the reply.

The CLI reports every assistant turn's stop_reason on `message_delta`:
`tool_use` for the sentence it writes before reaching for a tool, `end_turn`
for the answer. Only the latter may reach the reply — narration streamed as
`token` events would be appended to the answer and spoken aloud by the TTS
queue, which reads whatever the token stream gives it.
"""
import asyncio
import json

import pytest

from suni.models import claude_code_agent as cca
from suni.tools import claude_code_advanced as ccadv


def _ev(**kw):
    return json.dumps({"type": "stream_event", "event": kw})


def _text(s):
    return _ev(type="content_block_delta", delta={"type": "text_delta", "text": s})


def _stop(reason):
    return _ev(type="message_delta", delta={"stop_reason": reason})


LINES = [
    json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"}),
    _ev(type="message_start"),
    # A thinking block: same delta channel, never part of the reply.
    _ev(type="content_block_delta", delta={"type": "thinking_delta", "thinking": "hmm"}),
    _text("Vou ver os ficheiros."),
    _ev(type="content_block_start", content_block={"type": "tool_use", "name": "Bash"}),
    _stop("tool_use"),
    _ev(type="message_start"),
    _text("Encontrei dois"),
    _text(" clientes."),
    _stop("end_turn"),
    json.dumps({"type": "result", "subtype": "success",
                "result": "Encontrei dois clientes.", "session_id": "sess-2"}),
]


def _run(monkeypatch, lines=LINES):
    events = []

    async def fake_stream(args, on_line, timeout=300, cwd=None, stdin_data=None):
        for line in lines:
            on_line(line)
        return 0, "\n".join(lines), ""

    monkeypatch.setattr(ccadv, "_run_claude_stream", fake_stream)
    rc, out, err, state = asyncio.run(
        cca._stream_run(["--print"], events.append, 30, "task"))
    return events, state


def test_only_the_answer_is_streamed_as_tokens(monkeypatch):
    events, _ = _run(monkeypatch)
    answer = "".join(e["text"] for e in events if e["type"] == "token")
    assert answer == "Encontrei dois clientes."
    assert "Vou ver" not in answer, "narration must never reach the reply"
    assert "hmm" not in answer, "thinking must never reach the reply"


def test_narration_and_tool_names_are_progress_notes(monkeypatch):
    events, _ = _run(monkeypatch)
    notes = [e["text"] for e in events if e["type"] == "cc_note"]
    assert "Vou ver os ficheiros." in notes
    assert any("Bash" in n for n in notes)


def test_result_line_is_authoritative_and_session_is_kept(monkeypatch):
    _, state = _run(monkeypatch)
    assert state["result"] == "Encontrei dois clientes."
    assert state["session_id"] == "sess-2", "--resume depends on this"


def test_no_result_line_falls_back_to_buffered_parsing(monkeypatch):
    # A crashed run has no result line: _stream_run returns {} so the caller
    # parses stdout the old way instead of reporting an empty answer.
    _, state = _run(monkeypatch, lines=LINES[:-1])
    assert state == {}
