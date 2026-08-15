---
name: Summarise a web page or article
slug: summarize-webpage
category: research
description: Fetch a URL, strip boilerplate, and produce a clean summary with key points.
version: 1
tool_count: 2
---

# Summarise a web page or article

Use when the user pastes a link and asks what it says.

## Steps
1. Call `web_fetch` on the URL.
2. Ignore nav/ads/boilerplate; focus on the main article body.
3. Produce: a one-line gist, 3-6 key points, and any notable figures/quotes (attributed).
4. If the page failed to load or is paywalled, say so plainly and offer alternatives.
