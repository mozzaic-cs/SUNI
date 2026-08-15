---
name: Research a topic and email a summary
slug: research-and-email-summary
category: research
description: Search the web on a topic, read the best sources, synthesise a concise briefing, and email it.
version: 1
tool_count: 4
---

# Research a topic and email a summary

Use when the user asks you to "look into X and send me a summary / email me what you find".

## Steps
1. **Clarify scope** — note the topic, the recipient (default to the user's own
   address), and any angle (news, comparison, how-to).
2. **Search** — call `web_search` with 2–3 focused queries covering the topic.
3. **Read the best sources** — call `web_fetch` on the 2–4 most relevant/authoritative
   results. Prefer primary sources over aggregators.
4. **Synthesise** — write a briefing: a one-line takeaway, then 3–6 bullet points of
   the key facts, then a short "sources" list (title — URL). Keep it tight; no filler.
5. **Send** — call `send_email` with a clear subject (e.g. "Briefing: <topic>") and the
   briefing as the body. Confirm to the user what you sent and to whom.

## Notes
- Always fetch live content before summarising — do not answer current-events topics
  from memory.
- Never paste raw article text as if it were your own analysis; attribute and condense.
