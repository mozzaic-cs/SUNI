---
name: Compile a status report
slug: status-report
category: business
description: Pull recent project activity into a dated status report, then deliver it as PDF or email.
version: 1
tool_count: 4
---

# Compile a status report

Use for "weekly update", "status report", "what happened this week", or a
recurring summary for a team or client.

## Steps
1. **Establish the window and the audience.** If the user has not said, ask
   before writing: a report for a client and one for the team contain different
   things. Default to the last 7 days.
2. **Gather** — `project_list` for active projects, then `project_get` on the
   relevant ones for their logs and status. Add `search_knowledge_base` if the
   report should cover indexed documents too.
3. **Structure it** — Headline (one sentence on overall state), Done, In
   progress, Blocked / needs a decision, Next. Put Blocked above Next: the
   reason someone reads a status report is to find what needs them.
4. **Draft plainly.** Each item: what changed, and what it means. Say "no
   movement this week" where that is true — a report that quietly omits a stalled
   item reads as progress.
5. **Deliver** — offer `create_pdf` for something to circulate, or `send_email`
   to a named recipient. Ask before sending; do not pick recipients unprompted.

## Notes
- If there is no activity in the window, say so in one line rather than padding
  the report with restated background.
- Dates: state the window explicitly at the top ("18-24 August"), so the report
  still makes sense when read later.
- Keep the audience's vocabulary. Internal shorthand does not belong in a client
  report.
