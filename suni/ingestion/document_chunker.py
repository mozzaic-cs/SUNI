"""
Text chunker — splits document pages into overlapping token windows.

Strategy: word-based sliding window.
  chunk_size    words per chunk   (default 380 ≈ 400 tokens at 1.05 words/token)
  overlap       words of overlap  (default 50 ≈ 50-token bridge between chunks)

Each chunk includes a short excerpt trimmed to the nearest sentence boundary
(A5: never truncated mid-sentence) for clean display in search results and
context injection without fetching the full file.
"""
from __future__ import annotations
import re

_WORDS_PER_TOKEN = 1.05   # approximate ratio for English/Portuguese

# Sentence-ending punctuation used by _smart_excerpt
_SENT_END = re.compile(r'[.!?…\n]')


def _smart_excerpt(text: str, max_chars: int = 500) -> str:
    """
    Return up to max_chars of text, trimmed to the nearest sentence boundary.

    Priority order:
      1. Last sentence-ending punctuation (.!?\\n) within max_chars
      2. Last word boundary (space) within max_chars
      3. Hard truncate at max_chars (fallback, never mid-word if possible)

    This avoids the '…context truncated mid-sentence' artifact in KB search
    results and injected doc excerpts (A5 fix).
    """
    if len(text) <= max_chars:
        return text

    window = text[:max_chars]

    # Find the last sentence boundary in the window
    last_sent = -1
    for m in _SENT_END.finditer(window):
        last_sent = m.end()

    if last_sent > max_chars // 3:          # meaningful sentence found
        return window[:last_sent].rstrip()

    # Fall back to last word boundary
    last_space = window.rfind(' ')
    if last_space > max_chars // 2:
        return window[:last_space].rstrip()

    return window.rstrip()


def chunk_pages(
    pages: list[dict],
    chunk_words: int = 380,
    overlap_words: int = 50,
) -> list[dict]:
    """
    Takes loader output (list of {text, page, metadata}) and returns
    a flat list of chunks ready for embedding:

      {text, excerpt, page, chunk_index, total_chunks_in_page}
    """
    chunks: list[dict] = []
    for page in pages:
        page_text = page.get("text", "").strip()
        if not page_text:
            continue
        page_chunks = _split(page_text, chunk_words, overlap_words)
        for i, chunk_text in enumerate(page_chunks):
            chunks.append({
                "text":                  chunk_text,
                "excerpt":               _smart_excerpt(chunk_text, 500),
                "page":                  page.get("page", 1),
                "chunk_index":           i,
                "total_chunks_in_page":  len(page_chunks),
                "page_metadata":         page.get("metadata", {}),
            })
    return chunks


def _split(text: str, chunk_words: int, overlap: int) -> list[str]:
    words = text.split()
    if not words:
        return []

    step = max(1, chunk_words - overlap)
    result = []
    for start in range(0, len(words), step):
        chunk = " ".join(words[start : start + chunk_words])
        if chunk:
            result.append(chunk)
        if start + chunk_words >= len(words):
            break
    return result
