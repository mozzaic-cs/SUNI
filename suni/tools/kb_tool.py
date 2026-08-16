"""
Knowledge Base search tool — explicit FAISS vector search over indexed documents.

Gives SUNI intentional control over KB queries, complementing the automatic
context injection in MemoryManager.build_context(). Use a lower similarity
threshold (0.15) than the auto-injection (0.30) to return results for
exploratory queries that may not have strong semantic overlap.
"""
from __future__ import annotations
import asyncio

SCHEMA = {
    "name": "search_knowledge_base",
    "description": (
        "Search the indexed Knowledge Base (a vector index over your document folders). "
        "Returns the most relevant document excerpts for your query. "
        "Use this for questions about company projects, files, reports, or any content from the "
        "indexed document store. "
        "IMPORTANT: do NOT use run_shell, list_files, or filesystem_list_directory to look for "
        "KB content — the Knowledge Base is a FAISS vector index, not a filesystem folder."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language description of what you are looking for",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (1–20, default 8)",
            },
        },
        "required": ["query"],
    },
}

_doc_store = None


def set_doc_store(store) -> None:
    global _doc_store
    _doc_store = store


async def search(query: str, top_k: int = 8) -> str:
    if _doc_store is None:
        return "Knowledge Base is not available (document store not initialised)."
    top_k = min(max(1, int(top_k)), 20)
    try:
        # _get_model_384, not _get_model: the function was renamed when the
        # embedding backend became configurable and this caller was missed, so
        # every search_knowledge_base call failed with an ImportError the model
        # reported as "an issue with the Knowledge Base search function".
        # The doc store is a 384-dim FAISS index, so the query must be embedded
        # with the same 384-dim model — not the 768-dim episodic embedder.
        from ..memory.manager import _get_model_384
        loop = asyncio.get_event_loop()
        vec = await loop.run_in_executor(None, lambda: _get_model_384().encode(query).tolist())
        results = _doc_store.search(vec, top_k=top_k, threshold=0.15)
        if not results:
            return f"No documents found in the Knowledge Base for: {query!r}"
        lines = [f"Knowledge Base results for: {query!r} ({len(results)} chunks)\n"]
        for i, r in enumerate(results, 1):
            src = f"{r.get('file_name', 'unknown')} p.{r.get('page', '?')}"
            score = r.get("score", 0)
            lines.append(f"[{i}] {src}  (score {score:.2f})")
            lines.append(r.get("excerpt", ""))
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"Knowledge Base search error: {e}"
