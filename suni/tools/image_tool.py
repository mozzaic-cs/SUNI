"""
Local image generation via Stable Diffusion (Hugging Face `diffusers`).

Mirrors the approach used by the SUNIverse feed engine: a StableDiffusionPipeline
loaded in-process on the GPU (no external API server). The model is lazy-loaded on
first use and kept resident. Generation runs in a worker thread so it never blocks
the event loop.

Heavy/optional: needs `diffusers`, `torch`, `transformers`, `accelerate` (see
requirements-imagegen.txt). If they are not installed, the tool reports that
plainly instead of crashing.

The result includes an /api/files/serve URL so the Face stage displays the image.
"""
from __future__ import annotations
import os
import uuid
import asyncio
import logging
from datetime import datetime

log = logging.getLogger("suni.image")

_DEFAULT_NEG = ("text, watermark, logo, signature, blurry, low quality, "
                "deformed, ugly, duplicate, mutilated")


def _free_ollama_vram() -> None:
    """Unload Ollama-resident models so Stable Diffusion has room on a shared GPU
    (the 8 GB card can't hold a 7B LLM and SD at once). Ollama transparently
    reloads on the next chat/embed call."""
    try:
        import httpx
        from .. import config as _c
        _oh = _c.ollama_host()
        r = httpx.get(f"{_oh}/api/ps", timeout=5)
        for m in (r.json().get("models") or []):
            name = m.get("name") or m.get("model")
            if name:
                httpx.post(f"{_oh}/api/generate",
                           json={"model": name, "prompt": "", "keep_alive": 0}, timeout=15)
        log.info("[IMAGE] freed Ollama VRAM before generation")
    except Exception as e:
        log.debug("[IMAGE] could not free Ollama VRAM: %s", e)

SCHEMA = {
    "name": "generate_image",
    "description": (
        "Generate an image locally from a text prompt using Stable Diffusion on this "
        "machine's GPU. Use whenever the user asks to create, generate, draw, or make "
        "an image, picture, illustration, or artwork. Write a rich, descriptive prompt. "
        "The result includes a URL — include it in your reply so the image is shown on "
        "the stage."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Detailed description of the image to create."},
            "negative_prompt": {"type": "string", "description": "What to avoid (optional).", "default": ""},
            "width": {"type": "integer", "description": "Width in px (default 512).", "default": 512},
            "height": {"type": "integer", "description": "Height in px (default 512).", "default": 512},
        },
        "required": ["prompt"],
    },
}


def _generate_sync(prompt: str, negative_prompt: str, width: int, height: int) -> str:
    """Load SD, generate one image, then release its VRAM. On an 8 GB card shared
    with Ollama we don't keep the pipeline resident — we free Ollama first, generate,
    and free SD after, so neither starves the other."""
    from .. import config as _cfg
    from ..tools.registry import USER_ID_CTX
    from ..user_settings import resolve_output_dir
    import torch
    from diffusers import StableDiffusionPipeline

    model  = str(_cfg.get("image_gen_model", "runwayml/stable-diffusion-v1-5"))
    device = str(_cfg.get("image_gen_device", "cuda"))
    use_cuda = (device == "cuda" and torch.cuda.is_available())

    if use_cuda:
        torch.cuda.empty_cache()
        try:
            free_b, _ = torch.cuda.mem_get_info()
        except Exception:
            free_b = 0
        # SD 1.5 (fp16) needs ~2.5 GB. Only disturb Ollama when the card is too
        # tight — unloading it destabilises the memory-embedding backend briefly.
        if free_b < 3 * 1024**3:
            _free_ollama_vram()
            torch.cuda.empty_cache()

    log.info("[IMAGE] loading Stable Diffusion '%s' (%s) ...", model, "cuda" if use_cuda else "cpu")
    dtype = torch.float16 if use_cuda else torch.float32
    _kw = dict(torch_dtype=dtype, safety_checker=None, requires_safety_checker=False)
    try:
        # Load from the local cache WITHOUT contacting the HF Hub — the online
        # version check otherwise adds ~3 minutes per load on this machine.
        pipe = StableDiffusionPipeline.from_pretrained(model, local_files_only=True, **_kw)
    except Exception:
        # Not cached yet → allow a one-time download.
        log.info("[IMAGE] model not cached — downloading once")
        pipe = StableDiffusionPipeline.from_pretrained(model, **_kw)
    pipe = pipe.to("cuda" if use_cuda else "cpu")
    if use_cuda:
        # We free Ollama's VRAM first, so SD has room to run at full speed.
        # Only fall back to aggressive attention slicing if the card is still tight.
        try:
            import torch as _t
            free_b, _ = _t.cuda.mem_get_info()
            if free_b < 3 * 1024**3:          # < 3 GB free → save memory (slower)
                pipe.enable_attention_slicing(1)
                log.info("[IMAGE] low VRAM — attention slicing on")
        except Exception:
            pass
    pipe.set_progress_bar_config(disable=True)

    try:
        steps = int(_cfg.get("image_gen_steps", 25) or 25)
        image = pipe(
            prompt=prompt,
            negative_prompt=negative_prompt or _DEFAULT_NEG,
            width=int(width), height=int(height),
            num_inference_steps=steps, guidance_scale=7.5,
        ).images[0]

        out_dir = resolve_output_dir(USER_ID_CTX.get(""))
        os.makedirs(out_dir, exist_ok=True)
        fn = f"suni_img_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}.png"
        fp = os.path.join(out_dir, fn)
        image.save(fp, format="PNG")
        log.info("[IMAGE] saved %s", fp)
        return fp
    finally:
        # Release SD's VRAM so Ollama can reclaim the card for chat/embeddings.
        try:
            del pipe
            if use_cuda:
                torch.cuda.empty_cache()
        except Exception:
            pass


async def handler(prompt: str, negative_prompt: str = "",
                  width: int = 512, height: int = 512, **_) -> str:
    from .. import config as _cfg
    if not bool(_cfg.get("image_gen_enabled", True)):
        return "Image generation is disabled (enable image_gen_enabled in config)."
    if not (prompt or "").strip():
        return "No prompt provided for image generation."
    try:
        import diffusers  # noqa: F401
    except Exception:
        return ("Image generation is not available — the local Stable Diffusion stack "
                "isn't installed. Install it with:  pip install -r requirements-imagegen.txt")
    try:
        fp = await asyncio.to_thread(_generate_sync, prompt, negative_prompt, width, height)
    except Exception as e:
        log.error("[IMAGE] generation failed: %s", e)
        return f"Image generation failed: {e}"
    # Serve URL → the Face stage displays it; token is injected by the response layer.
    return (f"Here is the image I generated.\n\n/api/files/serve?path={fp}")
