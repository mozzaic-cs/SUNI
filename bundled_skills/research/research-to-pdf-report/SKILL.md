---
name: Compile a research topic into a PDF report
slug: research-to-pdf-report
category: research
description: Gather information from the web and the knowledge base, then produce a structured PDF report.
version: 1
tool_count: 4
---

# Compile a research topic into a PDF report

Use when the user asks for a "report", "write-up", or "put it in a PDF" on a topic.

## Steps
1. **Gather** — call `search_knowledge_base` first (indexed documents may already
   cover it), then `web_search` + `web_fetch` for anything current or external.
2. **Outline** — decide sections: Summary, Background, Key Findings, Details,
   Sources. Adapt to the topic.
3. **Draft** — write clear Markdown-style content per section. Cite source file names
   (KB) and URLs (web) inline where a claim comes from a specific source.
4. **Create the PDF** — call `create_pdf(content, path, title)`, saving to the
   configured output directory with a descriptive filename.
5. **Deliver** — tell the user where the file was saved; offer to email it if useful.

## Notes
- Prefer knowledge-base content for anything about the user's own documents/projects.
- Keep claims grounded in what the sources actually say; flag gaps rather than guessing.
