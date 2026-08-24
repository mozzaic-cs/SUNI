---
name: Threat-model a codebase
slug: threat-model
category: development
description: Map a project's entry points, trust boundaries and secrets, and report what an attacker would try.
version: 1
tool_count: 3
---

# Threat-model a codebase

Use for "threat model", "what's the attack surface", "is this safe to expose",
or before putting something on a public network. This is about the shape of the
system; `code-review` covers defects in a specific change.

## Steps
1. **Map the surface** — `list_files` for the project layout, then `read_file`
   on entry points: HTTP routes, CLI arguments, scheduled jobs, message handlers,
   webhooks, file uploads. Anything that accepts input from outside the process.
2. **Find the trust boundaries.** For each entry point: who can reach it
   (anonymous / authenticated / admin), and what it can do once reached. Note
   where an authorization check is missing rather than assuming one exists
   upstream.
3. **Locate the secrets and the data.** Where credentials are read from, where
   personal data is stored, and what leaves the machine — outbound HTTP, email,
   log shipping, backups.
4. **Delegate the deep pass** — `claude_task`: ask it to review the entry points
   found above for injection, authentication and authorization gaps, unsafe
   deserialization, path traversal, SSRF and secrets in source or logs, and to
   cite file:line.
5. **Report by exploitability, not by category.** For each finding: who could do
   it, what they would need, and what they would get. A theoretical issue behind
   an admin login ranks below an unauthenticated one.
6. Offer to write the model to a file so it can be re-checked after changes.

## Notes
- Say plainly what was NOT examined. A threat model presented as complete when
  it skipped the auth layer is worse than none, because it stops the next look.
- Deployment context changes everything: bound to localhost, behind a VPN, or
  facing the internet are three different systems. Ask if unstated.
- Requires the `claude_task` tool (power-user/admin role) for step 4; without it,
  do steps 1-3 and say the deep pass was skipped.
