"""
SUNI runtime configuration — persisted to memory/suni_config.json.
All modules read from this; changes apply on next relevant call.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(os.environ.get("SUNI_CONFIG_PATH", "memory/suni_config.json"))

DEFAULTS: dict[str, Any] = {
    # False until the admin finishes the first-run wizard at /setup. Creating
    # the admin account is not the same as being configured: a fresh instance
    # still has no model chosen and no idea what language to answer in.
    "setup_completed":        False,
    "display_name":           "SUNI",
    "tagline":                "The world, as seen by SUNI.",
    # Empty = auto: use the best model installed at the core tier. Which model
    # to run belongs to the deployment, not the source — a hardcoded default is
    # wrong for anyone whose hardware or model library differs. Set it in the
    # admin panel (Configuration → Model).
    "model":                  "",
    "tts_voice":              "en-GB-SoniaNeural",
    "num_ctx":                8192,
    "email_poll_interval":    1800,
    "memory_top_k":           5,
    "max_tool_iterations":    8,
    "silence_ms":             2500,
    "system_prompt":          "",      # full base-persona override; "" = built-in generic default
    "claude_code_persona":    "",      # Claude Code tier persona override; "" = generic default
    "system_prompt_addendum": "",
    "stt_language":           "en-GB",
    "response_language":      "",      # "" = follow stt_language; or explicit e.g. "pt-PT"
    # Empty = auto: SUNI_OLLAMA_HOST / OLLAMA_HOST from the environment, else
    # http://localhost:11434. A hardcoded default here would win over the
    # environment and make the app unconfigurable wherever Ollama is not on
    # localhost — a container, or any split deployment. See ollama_host().
    "ollama_endpoints":       [],      # Ollama hosts to scan for models
    "doc_paths":              [],      # list of folder paths to index
    "doc_scan_interval":      600,     # seconds between scans (10 minutes)
    "doc_chunk_words":        380,     # words per chunk
    "doc_chunk_overlap":      50,      # overlap words
    "doc_top_k":              5,       # document results injected per query
    "doc_kb_enabled":         True,    # Tier-3 doc KB retrieval; set False to skip the 384-dim embed + FAISS search (faster testing)
    "global_output_dir":      "",      # base directory for generated files; empty = Desktop fallback
    # ── Email (outbound SMTP + inbound IMAP) ──────────────────────────────
    # Set these from the admin panel so an operator never has to edit .env.
    # Each one OVERRIDES the matching SUNI_SMTP_* / SUNI_IMAP_* env var when
    # non-empty; leaving them blank keeps whatever the environment provides,
    # so an existing .env deployment keeps working untouched.
    "smtp_host":              "",      # overrides SUNI_SMTP_HOST
    "smtp_port":              587,     # overrides SUNI_SMTP_PORT
    "smtp_user":              "",      # overrides SUNI_SMTP_USER
    "smtp_pass":              "",      # overrides SUNI_SMTP_PASS (secret — never echoed by /api/config)
    "notify_to":              "",      # overrides SUNI_NOTIFY_TO; empty = send to smtp_user
    "imap_host":              "",      # overrides SUNI_IMAP_HOST (inbox watcher)
    "imap_port":              993,     # overrides SUNI_IMAP_PORT
    "force_claude_code":      False,  # bypass all local models; route every request to T5
    "claude_code_timeout":    300,     # seconds before a Claude Code (T5) call is aborted
    # ── Codex (OpenAI Codex CLI) — 2nd no-key frontier provider ───────────
    "codex_cmd_path":         "",      # optional explicit path to codex.exe; "" = auto-discover
    "codex_timeout":          300,     # seconds before a `codex exec` call is aborted
    # ── Multi-model collaboration (Mode 2 / "collaborate" conv_mode) ──────
    # Opt-in per message via the UI mode toggle. Capable models draft → critique
    # each other → synthesize the best answer. Mode 1 (tier pipeline) is untouched.
    # PRIVACY: these are cloud frontier models — Mode-2 data leaves the box.
    "collaborate_enabled":    True,    # master switch for the collaborate mode
    "collaborate_skip_critique": False,  # fast dial: draft→synthesize only (skip cross-critique)
    "collaborate_pool":       [],      # [] = default Claude Code + Codex; or [{provider,model,base_url,api_key,enabled}]
    # ── Pluggable model chain (admin-ordered provider preference) ─────────
    # User-defined, drag-ordered list of chat models / providers, top = first
    # choice. STORAGE + ADMIN UI ONLY right now — deliberately NOT wired into
    # request routing yet (the orchestrator still uses the param-based tier
    # ladder). Wiring it into escalation is the explicit next step. Each entry:
    #   {id, label, provider (ollama|vllm|openai|anthropic|gemini|claude-code),
    #    model, base_url, api_key, enabled}
    "model_chain":            [],
    # Opt-in: when True, the first ENABLED model_chain entry overrides the primary
    # model/provider (routing keeps the existing escalation to Claude Code). Off =
    # unchanged tier-ladder behaviour. NOTE: force_claude_code short-circuits the
    # tier ladder, so this only takes effect with force_claude_code = False.
    "model_chain_routing":    False,
    # ── Telegram channel (long-poll gateway) ─────────────────────────────
    # Reach SUNI from Telegram with NO public URL: when enabled, the server
    # long-polls getUpdates (calls OUT to Telegram), so it works from a local
    # machine behind NAT. Mutually exclusive with the /telegram webhook.
    # SECURITY: long-poll has no per-request secret, so `telegram_allowed_chats`
    # is the ONLY gate — it fails CLOSED. Empty list = nobody is authorized; an
    # unknown chat is told its own id so the owner can add it, and its message is
    # NOT run. Inbound messages always run at role "standard" (least-privilege).
    "telegram_enabled":       False,   # start the long-poll gateway at boot
    "telegram_bot_token":     "",      # from @BotFather; overrides TELEGRAM_BOT_TOKEN env
    "telegram_allowed_chats": [],      # allow-list of chat ids (strings); empty = fail closed
    # ── Discord channel (Gateway WebSocket) ──────────────────────────────
    # Reach SUNI from Discord with NO public URL: when enabled, the server
    # connects OUT to Discord's Gateway (WebSocket) — works from a local machine.
    # REQUIRES the privileged Message Content Intent toggled ON in the Discord
    # Developer Portal (else the gateway closes 4014). SECURITY: fail closed on
    # `discord_allowed_channels` — only listed channel ids are answered; unknown
    # channels are silently ignored (a DM gets its channel id so the owner can
    # add it). Inbound messages always run at role "standard" (least-privilege).
    "discord_enabled":          False,  # start the Discord gateway at boot
    "discord_bot_token":        "",     # bot token; overrides DISCORD_BOT_TOKEN env
    "discord_allowed_channels": [],     # allow-list of channel ids (strings); empty = fail closed
    # ── Slack channel (Socket Mode) ──────────────────────────────────────
    # Reach SUNI from Slack with NO public URL: Socket Mode connects OUT to Slack.
    # Needs TWO tokens — an app-level token (xapp-…, connections:write) to open
    # the socket, and a bot token (xoxb-…, chat:write) to send. SECURITY: fail
    # closed on `slack_allowed_channels` — only listed channel ids are answered;
    # unknown channels are ignored (a DM gets its channel id to allow-list).
    # Inbound messages always run at role "standard" (least-privilege).
    "slack_enabled":          False,  # start Socket Mode at boot
    "slack_app_token":        "",     # xapp-… (connections:write); overrides SLACK_APP_TOKEN env
    "slack_bot_token":        "",     # xoxb-… (chat:write); overrides SLACK_BOT_TOKEN env
    "slack_allowed_channels": [],     # allow-list of channel ids (strings); empty = fail closed
    # ── Memory consolidation ──────────────────────────────────────────────
    "memory_consolidate_interval_days": 7,     # weekly background consolidation pass
    "memory_dedup_threshold":           0.92,  # cosine ≥ this → fact/pref treated as duplicate
    "memory_conv_dedup_threshold":      0.95,  # cosine ≥ this → conversation entries deduped
    "memory_ageout_days":               0,     # deprecate conversation entries older than N days (0 = off)
    "memory_supersede":                 True,  # LLM-detect contradicting facts and supersede them
    # ── Intent judge (tool-call security review) ──────────────────────────
    "intent_judge":       False,  # LLM reviews tool calls for off-intent / injected steering (additive-only)
    "intent_judge_model": "",     # model for the judge; "" = use main `model`
    "output_guard":       False,  # scan tool RESULTS: redact secrets + annotate injection before they reach the model
    # ── Backend circuit breaker (Ollama liveness) ─────────────────────────
    "backend_breaker":           True,  # fast-fail + auto-recover when the local model backend is down
    "backend_breaker_threshold": 3,     # consecutive CONNECTION failures before opening
    "backend_probe_interval_s":  30,    # seconds between recovery probes of an open backend
    # ── vLLM backend (dual-mode) ──────────────────────────────────────────
    # Set vllm_base_url (e.g. http://gpu-1:8000/v1) to switch chat/generation to
    # vLLM; empty = Ollama (default, unchanged). Embeddings are NOT affected.
    "vllm_base_url": "",   # OpenAI-compatible base URL incl. /v1; empty = use Ollama
    "vllm_model":    "",   # model name the vLLM server serves
    "vllm_api_key":  "",   # optional; only if vLLM started with --api-key
    # ── Episodic-memory embeddings — configurable endpoint ────────────────
    # Decoupled from the chat backend (see project_vllm_backend). HOST-swap is
    # SAFE (same model/dims → existing vectors stay valid). Changing the MODEL
    # requires a re-embed of memory (reembed_memory.py) — a dimension guard
    # refuses writes on a dim mismatch rather than silently corrupting recall.
    "embed_backend":  "ollama",                   # ollama | openai
    "embed_base_url": "http://localhost:11434",    # ollama host, or an OpenAI-compatible base incl. /v1
    "embed_model":    "nomic-embed-text",          # keep nomic-compatible unless you re-embed
    "embed_api_key":  "",                          # for openai-compatible embed endpoints that require it
    # ── Speech-to-text ────────────────────────────────────────────────────
    # 'browser' (default) = Web Speech API in the browser (audio → Google, no
    # server involvement). 'whisper' = server-side transcription via a self-hosted
    # OpenAI-compatible audio endpoint (privacy/enterprise/offline — audio stays
    # in your infra). Separate from stt_language (the browser recognizer locale).
    "stt_backend":  "browser",                     # browser | whisper
    "stt_base_url": "",                            # OpenAI-compatible audio base incl. /v1 (e.g. http://gpu-3:9000/v1)
    "stt_model":    "whisper-1",                   # model name the whisper server expects
    "stt_api_key":  "",                            # optional
    # ── Vision (image understanding) ─────────────────────────────────────
    # Empty = off (image attachments fall back to "binary file" today's behavior).
    # Set to an OpenAI-compatible VLM endpoint (vLLM serving a vision model, etc.).
    # Ollama-native vision is NOT supported by this path (different image format).
    "vision_base_url": "",                         # OpenAI-compatible base incl. /v1
    "vision_model":    "",                         # VLM name the server serves
    "vision_api_key":  "",                         # optional
    # ── Image generation (local Stable Diffusion) ────────────────────────
    # 'diffusers' = in-process Hugging Face pipeline on the GPU (no server;
    # matches the SUNIverse feed engine). A future 'a1111'/'comfyui'/'openai'
    # backend would call an image-generation ENDPOINT instead. Needs the heavy
    # optional stack — see requirements-imagegen.txt.
    "image_gen_enabled": True,
    "image_gen_backend": "diffusers",              # diffusers | (future: a1111 | comfyui | openai)
    "image_gen_model":   "runwayml/stable-diffusion-v1-5",
    "image_gen_device":  "cuda",                   # cuda | cpu
    "image_gen_steps":   25,
    # ── Text-to-speech ────────────────────────────────────────────────────
    # 'edge' (default) = edge-tts (Microsoft cloud). 'server' = a self-hosted
    # OpenAI-compatible speech endpoint (audio stays in your infra). The voice
    # string comes from the existing tts_voice setting.
    "tts_backend":  "edge",                        # edge | server
    "tts_base_url": "",                            # OpenAI-compatible base incl. /v1 (e.g. http://gpu-5:8080/v1)
    "tts_model":    "tts-1",                       # model name the server expects
    "tts_api_key":  "",                            # optional
}

_cache: dict[str, Any] = {}


def load() -> dict[str, Any]:
    global _cache
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        # Migration: a config file that predates setup_completed belongs to an
        # instance that was configured before the first-run wizard existed.
        # Without this, upgrading would drag every existing admin through a
        # wizard for an instance that is already set up.
        if data and "setup_completed" not in data:
            data["setup_completed"] = True
        _cache = {**DEFAULTS, **data}
    except Exception:
        _cache = dict(DEFAULTS)
    return _cache


def save(data: dict[str, Any]) -> None:
    global _cache
    merged = {**DEFAULTS, **data}
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _cache = merged


def get(key: str, default: Any = None) -> Any:
    if not _cache:
        load()
    return _cache.get(key, DEFAULTS.get(key, default))


def all() -> dict[str, Any]:
    if not _cache:
        load()
    return dict(_cache)


# Load on import
load()


# ── Ollama endpoint ──────────────────────────────────────────────────────────
_OLLAMA_FALLBACK = "http://localhost:11434"


def ollama_host() -> str:
    """Base URL of the Ollama server, without a trailing slash.

    Resolution: the first configured endpoint, then SUNI_OLLAMA_HOST, then
    Ollama's own OLLAMA_HOST convention, then localhost.

    This exists because the URL was hardcoded as "http://localhost:11434" in
    six places — memory embedding, consolidation, the document scanner, the
    approval judge, the agent factory and the benchmark runner. Anywhere Ollama
    is not on localhost (a container, or a split deployment) chat would appear
    to work while consolidation and document scanning silently failed against
    an address nothing was listening on.

    Several of those sites froze the value in a default argument, evaluated at
    import, so even config could not move them. Call this at use time.
    """
    eps = get("ollama_endpoints") or []
    if eps:
        first = str(eps[0]).strip()
        if first:
            return first.rstrip("/")

    for var in ("SUNI_OLLAMA_HOST", "OLLAMA_HOST"):
        raw = (os.environ.get(var) or "").strip()
        if raw:
            # Ollama's own convention is bare "host:port"; accept both forms.
            if "://" not in raw:
                raw = "http://" + raw
            return raw.rstrip("/")

    return _OLLAMA_FALLBACK
