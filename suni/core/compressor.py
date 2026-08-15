"""
Real-time context window compression for SUNI.

Three-stage pipeline applied before each LLM inference when the estimated
token count exceeds COMPRESS_THRESHOLD:

  Stage 0 — Semantic deduplication  [A1]
    Detects near-duplicate TOOL/SYSTEM messages via word-bigram Jaccard
    similarity (threshold 0.82). Removes the shorter duplicate, keeping
    the more informative copy. Pure algorithmic — no LLM call, ~0ms.

  Stage 1 — Tool-result summarisation
    Any TOOL message longer than TOOL_SUMMARY_CHARS is replaced with a
    qwen-generated summary (~80 words). Runs for every oversized result.

  Stage 2 — Conversation roll-up
    If still over threshold after Stage 1, all USER/ASSISTANT/TOOL messages
    older than the last KEEP_EXCHANGES exchanges are summarised into a
    single compact SYSTEM message and removed from history.

The primary agent (qwen2.5:7b) is used for Stage 1 + 2 summarisation.
Each summary call adds ~1-2s latency but prevents the silent tool-call
failures that occur when context exceeds 6,000+ tokens.
"""
from __future__ import annotations
import re
import time
from rich.console import Console
from .context import Context
from .message import Message, Role
from ..logger import get_logger

console = Console(stderr=True)
_log = get_logger(__name__)

from ..system_profile import COMPRESS_THRESHOLD
TOOL_SUMMARY_CHARS  = 1600   # chars (~400 tokens) above which a tool result is summarised
KEEP_EXCHANGES      = 2      # recent USER/ASSISTANT exchanges preserved verbatim
DEDUP_THRESHOLD     = 0.82   # Jaccard similarity above which a message is considered duplicate


# ── Jaccard helpers (A1) ──────────────────────────────────────────────────────

def _bigrams(text: str) -> frozenset:
    """Word bigrams from lowercased text — fast approximate similarity key."""
    words = re.sub(r'\s+', ' ', text.lower()).split()
    if len(words) < 2:
        return frozenset()
    return frozenset((words[i], words[i + 1]) for i in range(len(words) - 1))


def _jaccard(a: str, b: str) -> float:
    """Bigram Jaccard similarity in [0, 1]. Cheap — no model call."""
    bg_a, bg_b = _bigrams(a), _bigrams(b)
    if not bg_a or not bg_b:
        return 0.0
    return len(bg_a & bg_b) / len(bg_a | bg_b)


def estimate_tokens(messages: list[Message]) -> int:
    """Fast token estimate: ~1 token per 4 characters (English/Portuguese)."""
    return sum(len(m.content) for m in messages) // 4


class ContextCompressor:

    async def compress(
        self, context: Context, agent, trace: list
    ) -> bool:
        """
        Compress context in-place if over threshold.
        Returns True if any compression was applied.
        """
        msgs = context.history
        before = estimate_tokens(msgs)
        if before <= COMPRESS_THRESHOLD:
            return False

        ts = time.perf_counter()
        console.print(
            f"  [yellow]→ context compress: {before:,} estimated tokens[/yellow]"
        )
        _log.info("[COMPRESS] triggered at ~%d tokens", before)

        # Stage 0: semantic deduplication (A1 — zero LLM cost)
        dedup_removed = self._deduplicate(context)
        if dedup_removed:
            _log.info("[COMPRESS] dedup removed %d duplicate message(s)", dedup_removed)

        after_s0 = estimate_tokens(context.history)

        # Stage 1: summarise large tool results
        await self._summarise_tool_results(context, agent)

        after_s1 = estimate_tokens(context.history)
        _log.info("[COMPRESS] after tool-result summarisation: ~%d tokens", after_s1)

        # Stage 2: roll up old conversation if still over threshold
        if after_s1 > COMPRESS_THRESHOLD:
            await self._roll_up(context, agent)
            after_s2 = estimate_tokens(context.history)
            _log.info("[COMPRESS] after roll-up: ~%d tokens", after_s2)

        elapsed = time.perf_counter() - ts
        after = estimate_tokens(context.history)
        saved = before - after
        console.print(
            f"  [green]→ compressed {before:,} → {after:,} tokens "
            f"(saved {saved:,}, dedup {dedup_removed}) in {elapsed:.1f}s[/green]"
        )
        trace.append(("ctx compress", elapsed, f"{before}→{after} tok"))
        return True

    # ──────────────────────────────────────────────────────────────────────
    # Stage 0 — Semantic deduplication (A1)
    # ──────────────────────────────────────────────────────────────────────

    def _deduplicate(self, context: Context) -> int:
        """
        Remove near-duplicate TOOL and memory-injected SYSTEM messages.
        Comparison uses word-bigram Jaccard — fast, no model call.
        When two messages exceed DEDUP_THRESHOLD, the shorter one is dropped.
        Returns count of removed messages.
        """
        removed = 0
        # Process TOOL messages first (most common source of duplicates)
        for role in (Role.TOOL, Role.SYSTEM):
            indices = [
                i for i, m in enumerate(context.history)
                if m.role == role and len(m.content) > 80
            ]
            to_remove: set[int] = set()
            for i in range(len(indices)):
                if indices[i] in to_remove:
                    continue
                for j in range(i + 1, len(indices)):
                    if indices[j] in to_remove:
                        continue
                    a_idx, b_idx = indices[i], indices[j]
                    a, b = context.history[a_idx].content, context.history[b_idx].content
                    if _jaccard(a, b) >= DEDUP_THRESHOLD:
                        # Drop the shorter (less informative) message
                        drop = a_idx if len(a) <= len(b) else b_idx
                        to_remove.add(drop)

            if to_remove:
                context.history = [
                    m for i, m in enumerate(context.history) if i not in to_remove
                ]
                removed += len(to_remove)

        return removed

    # ──────────────────────────────────────────────────────────────────────
    # Stage 1
    # ──────────────────────────────────────────────────────────────────────

    async def _summarise_tool_results(self, context: Context, agent) -> None:
        """Replace oversized TOOL messages with compact summaries."""
        for i, msg in enumerate(context.history):
            if msg.role != Role.TOOL:
                continue
            if len(msg.content) <= TOOL_SUMMARY_CHARS:
                continue
            summary = await self._summarise(
                msg.content, agent,
                instruction="Summarise this tool output in ≤60 words, preserving "
                            "all key facts, numbers, names, and file paths."
            )
            context.history[i] = Message(
                role=msg.role,
                content=f"[summarised] {summary}",
                tool_call_id=msg.tool_call_id,
                tool_name=msg.tool_name,
                agent=msg.agent,
            )

    # ──────────────────────────────────────────────────────────────────────
    # Stage 2
    # ──────────────────────────────────────────────────────────────────────

    async def _roll_up(self, context: Context, agent) -> bool:
        """
        Summarise old turns into a single SYSTEM message.
        Keeps the most recent KEEP_EXCHANGES USER/ASSISTANT pairs verbatim.
        """
        msgs = context.history

        # Find indices of USER messages (non-SYSTEM only)
        user_indices = [
            i for i, m in enumerate(msgs)
            if m.role == Role.USER
        ]

        if len(user_indices) <= KEEP_EXCHANGES:
            return False  # not enough history to roll up

        # Everything before the (KEEP_EXCHANGES + 1)th most recent USER message
        split_at = user_indices[-(KEEP_EXCHANGES + 1)]
        old_msgs = msgs[:split_at]
        recent_msgs = msgs[split_at:]

        # Only roll up if there's something worth compressing
        if not any(m.role in (Role.USER, Role.ASSISTANT) for m in old_msgs):
            return False

        # Build a readable dialogue for the summary prompt
        dialogue_lines = []
        for m in old_msgs:
            if m.role == Role.USER:
                dialogue_lines.append(f"User: {m.content[:400]}")
            elif m.role == Role.ASSISTANT:
                dialogue_lines.append(f"SUNI: {m.content[:400]}")
            elif m.role == Role.TOOL:
                name = getattr(m, "tool_name", "tool") or "tool"
                dialogue_lines.append(f"[{name} result]: {m.content[:200]}")

        dialogue = "\n".join(dialogue_lines)

        summary = await self._summarise(
            dialogue, agent,
            instruction="Summarise this conversation history in ≤150 words. "
                        "Preserve all specific facts: names, URLs, file paths, "
                        "email addresses, and task outcomes. Omit pleasantries."
        )

        summary_msg = Message(
            role=Role.SYSTEM,
            content=f"[Prior conversation summary]\n{summary}",
            agent="compressor",
        )

        context.history = [summary_msg] + list(recent_msgs)
        return True

    # ──────────────────────────────────────────────────────────────────────
    # Shared LLM call
    # ──────────────────────────────────────────────────────────────────────

    async def _summarise(self, text: str, agent, instruction: str) -> str:
        prompt = f"{instruction}\n\n{text[:4000]}"
        msgs = [Message(role=Role.USER, content=prompt)]
        try:
            result = await agent.chat(msgs, Context(), tools=None)
            return result.content.strip()
        except Exception as e:
            _log.warning("[COMPRESS] summarisation failed: %s", e)
            return text[:300] + "…[summary failed]"
