"""ClaudeCodeAgent — wraps the Claude Code CLI as a BaseAgent for T5 direct routing."""
from __future__ import annotations
import os
from ..core.base_agent import BaseAgent
from ..core.message import Message, Role
from ..core.context import Context

# Generic built-in persona for the Claude Code tier. An owner-supplied
# `claude_code_persona` config value overrides it (see _cc_persona()).
_SUNI_CC_PERSONA_DEFAULT = (
    "You are SUNI — Synthetic Unit of Networked Intelligence. "
    "You are a personal AI assistant to your user. "
    "You are not a software developer and are not working on any codebase. "
    "Be concise. When you run tools, report the results plainly — no preamble."
)


def _cc_persona() -> str:
    from .. import config as _cfg
    return str(_cfg.get("claude_code_persona", "") or "").strip() or _SUNI_CC_PERSONA_DEFAULT


def _task_suffix(out_dir: str) -> str:
    """The operating notes appended to every Claude Code task.

    Two of them hand over SUNI's own plumbing: where generated files belong, and
    where the KB index lives. Those are the means, not the answer — so the third
    block tells the CLI to keep them out of its reply.

    It leaked once. Asked about a company it had no documents for, SUNI opened
    with "Nada nos seus documentos indexados — procurei em doc_meta.json,
    doc_scan.json e na memória", narrating internal layout at a user who wanted
    a yes or no. The persona is the wrong place to fix that: it is overridable
    from config, so an install with its own persona would never receive it. This
    suffix is code and always applies.

    The rule is behavioural rather than a list of filenames, deliberately. The
    leak named `doc_scan.json`, which nothing here mentions — the CLI found it by
    listing `memory/`. A denylist of the names we happen to inject would have
    missed it, and spelling names out inside a prohibition invites echoing them.
    """
    return (
        f"\n\n[File rule: Save any output files to {out_dir} — not to the SUNI "
        "install directory.]"
        "\n\n[Knowledge Base: indexed documents are catalogued in "
        "memory/doc_meta.json (fields: file_path, file_name, page, excerpt, mtime). "
        "Search it (e.g. grep) to find relevant files. All indexed source paths "
        "are directly readable on this machine via Read/Bash — open or copy them directly, "
        "do not ask the user to map a drive.]"
        "\n\n[Reporting rule: the notes above are SUNI's internal plumbing, not part "
        "of the answer. Report what you found, or that you found nothing — never "
        "where you looked. Do not name or describe SUNI's internal files, indexes, "
        "directories or install paths in your reply, and do not narrate your search "
        "process.]"
    )

_CC_HOME = os.path.expanduser("~")
_CC_TOOLS = "Read,Glob,Grep,WebFetch,WebSearch,Bash"


async def _stream_run(args: list[str], event_cb, timeout: int,
                      task: str) -> tuple[int, str, str, dict]:
    """Run the CLI in stream-json mode, reporting progress as it arrives.

    Every assistant turn is classified by its stop_reason, which the CLI sends
    on `message_delta`:
      tool_use  — the model is narrating before it calls a tool. Progress, not
                  the answer: it goes out as `cc_note`, so it is never appended
                  to the reply and never reaches the text-to-speech queue.
      end_turn  — the answer. Streamed as `token` events, the same shape the
                  chat endpoint already sends, so the clients need no change.
    The `result` line stays authoritative for what is displayed and persisted;
    the streamed text is only what the user watches arrive.
    """
    import json as _json
    from ..tools.claude_code_advanced import _run_claude_stream

    state: dict = {"result": "", "session_id": ""}
    cur: list[str] = []

    def _flush_tokens(text: str) -> None:
        # Word-sized events, matching what the chat endpoint emitted before, so
        # the clients' caption and per-sentence TTS behave exactly as they did.
        words = text.split(" ")
        for i, w in enumerate(words):
            chunk = w + (" " if i < len(words) - 1 else "")
            if chunk:
                event_cb({"type": "token", "text": chunk})

    def _on_line(line: str) -> None:
        try:
            d = _json.loads(line)
        except ValueError:
            return
        t = d.get("type")
        if t == "system" and d.get("subtype") == "init":
            state["session_id"] = d.get("session_id", "") or state["session_id"]
        elif t == "result":
            if d.get("session_id"):
                state["session_id"] = d["session_id"]
            if isinstance(d.get("result"), str):
                state["result"] = d["result"]
        elif t == "stream_event":
            ev = d.get("event") or {}
            et = ev.get("type")
            if et == "message_start":
                cur.clear()
            elif et == "content_block_start":
                blk = ev.get("content_block") or {}
                if blk.get("type") == "tool_use" and blk.get("name"):
                    event_cb({"type": "cc_note", "text": f"· {blk['name']}"})
            elif et == "content_block_delta":
                delta = ev.get("delta") or {}
                # text_delta ONLY: thinking_delta and signature_delta ride the
                # same channel and are not part of the reply.
                if delta.get("type") == "text_delta":
                    cur.append(delta.get("text", ""))
            elif et == "message_delta":
                stop = (ev.get("delta") or {}).get("stop_reason")
                text = "".join(cur).strip()
                cur.clear()
                if not text:
                    return
                if stop == "end_turn":
                    _flush_tokens(text)
                else:
                    event_cb({"type": "cc_note", "text": text})

    rc, stdout, stderr = await _run_claude_stream(
        args, _on_line, timeout=timeout, cwd=_CC_HOME, stdin_data=task)
    return rc, stdout, stderr, (state if state.get("result") else {})


class ClaudeCodeAgent(BaseAgent):
    def __init__(self, name: str = "claude-code"):
        super().__init__(name)

    async def chat(
        self,
        messages: list[Message],
        context: Context,
        tools: list[dict] | None = None,
    ) -> Message:
        from ..tools.claude_code_advanced import _run_claude, _parse_json_output

        user_msgs = [m for m in messages if m.role == Role.USER]
        if not user_msgs:
            return Message(role=Role.ASSISTANT, content="No task provided.", agent=self.name)
        task = user_msgs[-1].content

        conversation_id = context.get("conversation_id")
        cc_session_id = context.get("cc_session_id")
        if not cc_session_id and conversation_id:
            from .. import conversations as _conversations
            cc_session_id = _conversations.get_cc_session(conversation_id)
            if cc_session_id:
                context.set("cc_session_id", cc_session_id)

        prefix_parts = []

        # Global memory/preferences/language context built by the orchestrator —
        # folded into every turn so CC sees relevant KB/episodic hits each time.
        for m in messages:
            if m.role == Role.SYSTEM and m.agent in ("memory", "lang") and m.content:
                prefix_parts.append(m.content)

        # User-attached files. The chat handler injects these as SYSTEM messages
        # tagged agent="upload" carrying the file's absolute path + extracted text.
        # Without forwarding them here, uploads were silently dropped on the T5
        # path — CC never saw the file. Pass the content through and tell CC it can
        # read/parse the real file (spreadsheets/binaries via Bash + pandas/openpyxl).
        _uploads = [
            m for m in messages
            if m.role == Role.SYSTEM and m.agent == "upload" and m.content
        ]
        for m in _uploads:
            prefix_parts.append(m.content)
        if _uploads:
            prefix_parts.append(
                "[The attached file(s) above exist on the local filesystem at the "
                "path given for each. Open them with Read, or for spreadsheets and "
                "other binary formats parse them with Bash (e.g. python with "
                "pandas/openpyxl) to get the full structure and every cell.]"
            )

        # On the first CC turn in a conversation, prepend recent context so CC isn't blind
        if not cc_session_id:
            prior = [m for m in messages if m.role in (Role.USER, Role.ASSISTANT)][:-1]
            if prior:
                ctx_lines = [
                    f"{'User' if m.role == Role.USER else 'Assistant'}: {m.content[:300]}"
                    for m in prior[-4:]
                ]
                prefix_parts.append("[Prior conversation]\n" + "\n".join(ctx_lines))

        if prefix_parts:
            task = "\n\n".join(prefix_parts) + "\n\n" + task

        # Output directory + Knowledge Base notes, and the rule that keeps both
        # out of the reply. See _task_suffix.
        from ..tools.registry import USER_ID_CTX as _UID_CTX
        from ..user_settings import resolve_output_dir as _rod
        task = task + _task_suffix(_rod(_UID_CTX.get("")))

        # The prompt goes through stdin (see _run_claude): passing it as a --print
        # argument routes it through the Windows cmd.exe shim, whose ~8191-char
        # command-line limit overflows on large prompts ("The command line is too
        # long"). stdin has no such cap and preserves newlines, so no flattening.
        # Audit: the CLI chooses its own model, so the honest record is the
        # delegation itself — recorded BEFORE the subprocess runs, because a run
        # that times out must still show that work left for Claude Code.
        from .. import usage as _usage
        _usage.record_model("claude-code (CLI, model chosen by the CLI)")

        from ..tools.claude_code_advanced import EVENT_CB_CTX, _run_claude_stream
        _event_cb = EVENT_CB_CTX.get()

        args = [
            "--print",
            "--output-format", "json",
            "--system-prompt", _cc_persona(),
            "--allowedTools", _CC_TOOLS,
        ]
        if _event_cb:
            # Stream mode: the CLI reports as it goes, so a multi-minute run
            # stops looking like a hang. --verbose is REQUIRED by the CLI
            # alongside stream-json under --print.
            args[1:3] = ["--output-format", "stream-json", "--verbose",
                         "--include-partial-messages"]
        if cc_session_id:
            args += ["--resume", cc_session_id]

        from .. import config as _cfg
        _cc_timeout = int(_cfg.get("claude_code_timeout", 300) or 300)

        if _event_cb:
            rc, stdout, stderr, _streamed = await _stream_run(
                args, _event_cb, _cc_timeout, task
            )
        else:
            _streamed = {}
            rc, stdout, stderr = await _run_claude(
                args, timeout=_cc_timeout, cwd=_CC_HOME, stdin_data=task)

        if rc != 0 and not stdout.strip():
            content = (
                f"Claude Code returned an error (exit {rc}): "
                f"{stderr.strip() or 'no output'}"
            )
        else:
            parsed = _streamed or _parse_json_output(stdout)
            content = parsed.get("result", parsed.get("content", stdout.strip()))
            new_sid = parsed.get("session_id", "")
            if new_sid:
                context.set("cc_session_id", new_sid)
                if conversation_id and new_sid != cc_session_id:
                    from .. import conversations as _conversations
                    _conversations.set_cc_session(conversation_id, new_sid)

        return Message(role=Role.ASSISTANT, content=content, agent=self.name)
