---
name: Debug an issue systematically
slug: debug-systematically
category: development
description: Apply a structured reproduce, isolate, hypothesise, test, fix method to a bug.
version: 1
tool_count: 2
---

# Debug an issue systematically

Use for fixing a bug or diagnosing something broken.

## Steps
1. **Reproduce** - get exact steps, inputs, and the error/stack.
2. **Isolate** - narrow to the smallest failing case; check recent changes.
3. **Hypothesise** - list the most likely causes, ranked.
4. **Test** each hypothesis with a targeted check (`run_shell` / `claude_task`).
5. **Fix + verify** - apply the fix and confirm the repro now passes. Explain the root cause.
