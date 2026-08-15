---
name: Extract and summarise a PDF
slug: extract-pdf-text
category: documents
description: Pull the text out of a PDF and summarise or answer questions about it.
version: 1
tool_count: 2
---

# Extract and summarise a PDF

Use for reading or summarising a PDF document.

## Steps
1. Extract text via `run_shell` (e.g. Python pdfplumber/pypdf), page by page.
2. If it is a scan (no text layer), say so and suggest OCR.
3. Summarise per section, or answer the specific question citing page numbers.
4. Offer to save the summary as a new PDF.

## Prerequisite
- Needs a PDF text library (pdfplumber/pypdf) available to `run_shell`.
