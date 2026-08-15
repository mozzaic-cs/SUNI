---
name: Generate a changelog from git history
slug: changelog-from-git
category: development
description: Turn git commit history into clean, user-facing release notes.
version: 1
tool_count: 2
---

# Generate a changelog from git history

Use for writing release notes or a changelog.

## Steps
1. `run_shell`: `git log <lastTag>..HEAD --oneline` (or a date range) to get commits.
2. Group by type: Features, Fixes, Performance, Docs, Internal.
3. Rewrite each into user-facing language (what changed for the user, not the diff).
4. Output Markdown; offer to save to CHANGELOG.md or a PDF.
