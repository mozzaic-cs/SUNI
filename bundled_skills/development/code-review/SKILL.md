---
name: Review code changes
slug: code-review
category: development
description: Delegate a thorough code review to Claude Code and report prioritised findings.
version: 1
tool_count: 1
---

# Review code changes

Use for reviewing code, changes, or a PR.

## Steps
1. Delegate to `claude_task`: ask it to review the target files/diff for correctness,
   security, edge cases, and clarity - most-severe first, with file:line references.
2. Relay the findings grouped by severity; note which are blocking vs. nice-to-have.
3. Offer to apply the safe fixes (via `claude_task`) once the user picks.

## Note
- Requires the `claude_task` tool (power-user/admin role).
