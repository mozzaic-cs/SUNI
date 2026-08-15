---
name: Organise a messy folder
slug: organize-folder
category: productivity
description: Sort files by type/context, find duplicates, and propose a tidy structure.
version: 1
tool_count: 2
---

# Organise a messy folder

Use for cleaning up or organising a folder.

## Steps
1. `list_files` / `run_shell` to inventory the folder (names, sizes, dates, types).
2. Propose a structure (by type, project, or date) and flag likely duplicates.
3. **Confirm with the user before moving/renaming anything.**
4. On approval, execute via `run_shell`, then report what changed.

## Safety
- Never delete files without explicit confirmation; prefer moving to a review folder.
