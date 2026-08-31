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

_CC_HOME = os.path.expanduser("~")
_CC_TOOLS = "Read,Glob,Grep,WebFetch,WebSearch,Bash"


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

        # Append output-directory rule so generated files land in the right place
        from ..tools.registry import USER_ID_CTX as _UID_CTX
        from ..user_settings import resolve_output_dir as _rod
        out_dir = _rod(_UID_CTX.get(""))
        task = task + f"\n\n[File rule: Save any output files to {out_dir} — not to the SUNI install directory.]"

        # Knowledge Base access: indexed documents are catalogued in
        # memory/doc_meta.json (file_path, file_name, page, excerpt fields).
        # Indexed source paths are directly readable on this machine —
        # no drive mapping needed.
        task = task + (
            "\n\n[Knowledge Base: indexed documents are catalogued in "
            "memory/doc_meta.json (fields: file_path, file_name, page, excerpt, mtime). "
            "Search it (e.g. grep) to find relevant files. All indexed source paths "
            "are directly readable on this machine via Read/Bash — open or copy them directly, "
            "do not ask the user to map a drive.]"
        )

        # The prompt goes through stdin (see _run_claude): passing it as a --print
        # argument routes it through the Windows cmd.exe shim, whose ~8191-char
        # command-line limit overflows on large prompts ("The command line is too
        # long"). stdin has no such cap and preserves newlines, so no flattening.
        # Audit: the CLI chooses its own model, so the honest record is the
        # delegation itself — recorded BEFORE the subprocess runs, because a run
        # that times out must still show that work left for Claude Code.
        from .. import usage as _usage
        _usage.record_model("claude-code (CLI, model chosen by the CLI)")

        args = [
            "--print",
            "--output-format", "json",
            "--system-prompt", _cc_persona(),
            "--allowedTools", _CC_TOOLS,
        ]
        if cc_session_id:
            args += ["--resume", cc_session_id]

        from .. import config as _cfg
        _cc_timeout = int(_cfg.get("claude_code_timeout", 300) or 300)
        rc, stdout, stderr = await _run_claude(args, timeout=_cc_timeout, cwd=_CC_HOME, stdin_data=task)

        if rc != 0 and not stdout.strip():
            content = (
                f"Claude Code returned an error (exit {rc}): "
                f"{stderr.strip() or 'no output'}"
            )
        else:
            parsed = _parse_json_output(stdout)
            content = parsed.get("result", parsed.get("content", stdout.strip()))
            new_sid = parsed.get("session_id", "")
            if new_sid:
                context.set("cc_session_id", new_sid)
                if conversation_id and new_sid != cc_session_id:
                    from .. import conversations as _conversations
                    _conversations.set_cc_session(conversation_id, new_sid)

        return Message(role=Role.ASSISTANT, content=content, agent=self.name)
