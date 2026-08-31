"""
Image understanding via an OpenAI-compatible vision model (VLM).

Self-contained adapter (mirrors stt.py) — deliberately NOT routed through the
Message/agent machinery. Images are multi-MB base64 blobs; putting them on
`Message` would break context token-estimation/compression, get embedded into
episodic memory, and diverge across backends (Ollama's native vision uses a
different image format). So the adapter speaks raw OpenAI multimodal content
directly and images never enter Message/context/memory.

Scope (v1):
  - OpenAI-compatible VLM endpoints only (vLLM serving a vision model, etc.).
    Ollama-native vision is out of scope (different API); verify against the
    target or defer.
  - Single-turn: the raw image is used for THIS turn only; follow-ups work off
    the VLM's text answer (the image is not retained in context/memory).
  - Images sent as-is (no downscaling) — a large photo is a large request.

Reuses the host-keyed circuit breaker and per-request token accounting.
"""
from __future__ import annotations
import base64
import logging
from pathlib import Path

import httpx

from . import config as _cfg
from .models import health as _health

log = logging.getLogger("suni.vision")

_TIMEOUT = 90
# Extension → data-uri MIME. Only these are treated as images.
_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}
_MAX_IMAGES = 4


class VisionError(Exception):
    """User-facing vision failure."""


def enabled() -> bool:
    return bool(str(_cfg.get("vision_base_url", "") or "").strip())


def is_image(path: str) -> bool:
    return Path(path).suffix.lower() in _IMAGE_MIME


def _data_uri(path: str) -> str:
    mime = _IMAGE_MIME.get(Path(path).suffix.lower(), "application/octet-stream")
    b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def build_payload(image_paths: list[str], prompt: str, model: str) -> dict:
    """Construct the OpenAI multimodal chat request body (pure — unit-testable)."""
    content: list[dict] = [{"type": "text", "text": prompt or "Describe this image."}]
    for p in image_paths[:_MAX_IMAGES]:
        content.append({"type": "image_url", "image_url": {"url": _data_uri(p)}})
    return {"model": model, "messages": [{"role": "user", "content": content}]}


async def describe(image_paths: list[str], prompt: str) -> str:
    """Send image(s) + prompt to the configured VLM, return its text answer.
    Raises VisionError on any failure."""
    base = str(_cfg.get("vision_base_url", "")).strip()
    if not base:
        raise VisionError("Vision is not configured.")
    model   = str(_cfg.get("vision_model", "") or "")
    api_key = str(_cfg.get("vision_api_key", ""))
    host    = base

    if _health.enabled() and not _health.allow(host):
        raise VisionError("Vision backend is temporarily unavailable.")

    try:
        payload = build_payload(image_paths, prompt, model)
    except Exception as exc:
        raise VisionError(f"Could not read image: {exc}")

    url = base.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, json=payload, headers=headers)
    except Exception as exc:
        if _health.enabled() and _health.is_connection_failure(exc):
            _health.record_failure(host)
        raise VisionError(f"Vision request failed: {exc}")

    if _health.enabled():
        _health.record_success(host)

    if r.status_code != 200:
        log.warning("[VISION] endpoint %s returned %s", url, r.status_code)
        raise VisionError(f"Vision failed (HTTP {r.status_code}).")

    try:
        data = r.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        raise VisionError(f"Unexpected vision response: {exc}")

    # Per-request token accounting (same as the chat agents).
    try:
        from . import usage as _usage
        usage = data.get("usage") or {}
        _usage.record(usage.get("prompt_tokens"), usage.get("completion_tokens"))
        _usage.record_model(f"vision:{model}" if model else "vision")
    except Exception:
        pass

    return text
