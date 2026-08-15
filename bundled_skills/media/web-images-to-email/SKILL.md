---
name: Find images on the web and email them
slug: web-images-to-email
category: media
description: Search the web for images on a subject, download them, and email them as attachments.
version: 1
tool_count: 3
---

# Find images on the web and email them

Use when the user asks to "find pictures of X and send them to me / email me some images".

## Steps
1. **Search** — call `web_search` for the subject and identify direct image URLs
   (jpg/png/webp) from the results.
2. **Download** — call `download_file(url, path)` for each image, saving to a temp/
   downloads folder. Aim for 3–6 good images unless the user asked for a specific count.
3. **Email** — call `send_email` with `attachment_paths=[...]` listing every
   downloaded file, a clear subject, and a one-line body.

## Fallback
- If some downloads fail or the hosts block direct fetches, send the image URLs as
  links in the email body instead of attachments — do not silently drop them.

## Notes
- Verify each downloaded file is actually an image before attaching.
- Respect obvious copyright/attribution where the source requires it.
