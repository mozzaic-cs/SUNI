---
name: Write a new skill
slug: write-a-skill
category: productivity
description: Turn a task SUNI just solved into a reusable SKILL.md and save it.
version: 1
tool_count: 3
---

# Write a new skill

Use when the user says "remember how to do this", "make that a skill", or after
finishing a multi-step task worth repeating.

## Steps
1. **Check first** — call `skills_list` with a keyword from the task. If a close
   match exists, offer to refine that one instead of adding a near-duplicate.
   Every skill costs tokens on every request, so a second copy is a real cost.
2. **Name it** — a short imperative title ("Compile a weekly status report") and
   a slug in kebab-case. Pick an existing category if one fits: business,
   content, data, development, documents, email, knowledge, media, productivity,
   research.
3. **Write the description in one line.** This is the part injected into every
   conversation, so it must say what the skill does and when to reach for it —
   under about 100 characters. Everything else is only loaded on demand.
4. **Write the steps** — numbered, imperative, and naming the actual tools to
   call. Say what to do when a step fails or returns nothing, not just the happy
   path.
5. **Save** — call `skill_save` with the name, slug, category, description and
   the Markdown body.
6. Tell the user it is saved and that they can edit it in the admin panel.

## Notes
- Name only tools that exist. `skills_list` shows what other skills use; a step
  calling a tool SUNI does not have is a recipe that fails silently at the point
  of use.
- Good skills are specific. "Research a topic" is already covered; "compile the
  monthly supplier report and email it to the team" is worth saving.
- Prefer editing an existing skill over adding one. The catalogue is read on
  every turn.
