---
name: Summarise a YouTube video
slug: summarize-youtube-video
category: research
description: Fetch a video transcript and summarise it into key points and takeaways.
version: 1
tool_count: 2
---

# Summarise a YouTube video

Use for summarising a YouTube video into key points.

## Steps
1. Get the transcript. Prefer `run_shell` with `yt-dlp --write-auto-sub --skip-download`
   (or a transcript tool) to fetch captions; fall back to `web_fetch` of a transcript page.
2. Summarise into: topic, 5-8 key points in order, and 2-3 actionable takeaways.
3. Cite timestamps where the transcript provides them.

## Prerequisite
- Needs `yt-dlp` (or equivalent) available to `run_shell`. If missing, tell the user.
