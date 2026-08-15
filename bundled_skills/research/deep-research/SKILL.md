---
name: Deep multi-source research
slug: deep-research
category: research
description: Run an autonomous, multi-step research pass across several sources and synthesise findings.
version: 1
tool_count: 5
---

# Deep multi-source research

Use for open-ended "research X thoroughly" requests that need depth, not a single lookup.

## Steps
1. **Decompose** the question into 3-5 sub-questions.
2. For each, call `web_search`, then `web_fetch` the 1-2 best sources. Prefer primary
   sources (official docs, filings, papers) over aggregators.
3. Cross-check: where sources disagree, note it rather than picking one silently.
4. **Synthesise** into: Answer up front, then Findings by sub-question, then Open
   questions, then Sources (title - URL).
5. Offer to export as a PDF (`create_pdf`) or email it (`send_email`).

## Notes
- Track which claim came from which source; never merge them into unattributed prose.
