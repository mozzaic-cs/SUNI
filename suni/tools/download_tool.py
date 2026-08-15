"""Download a file from a URL and save it locally."""
from __future__ import annotations
import asyncio
from pathlib import Path

SCHEMA = {
    "name": "download_file",
    "description": (
        "Download a file from a URL and save it to a local path. "
        "Use this to download images, PDFs, or other files before attaching them to an email. "
        "For images, save to the user's Downloads folder by default."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url":  {"type": "string", "description": "URL to download"},
            "path": {"type": "string", "description": "Full local path to save the file, e.g. the Downloads folder"},
        },
        "required": ["url", "path"],
    },
}


def _download_sync(url: str, path: str) -> str:
    import requests
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, timeout=20,
                     headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                     allow_redirects=True)
    r.raise_for_status()
    # Validate it's actually a file (not an error page)
    content_type = r.headers.get("content-type", "")
    if "text/html" in content_type and not url.lower().endswith(".html"):
        return f"Download blocked: server returned HTML instead of file (content-type: {content_type})"
    dest.write_bytes(r.content)
    kb = round(len(r.content) / 1024, 1)
    return f"Downloaded {kb} KB → {path}"


async def handler(url: str, path: str) -> str:
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _download_sync, url, path)
    except Exception as e:
        return f"Download failed: {e}"
