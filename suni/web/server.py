"""
SUNI Web Server — FastAPI backend for the web UI.

Endpoints:
  GET  /            â†' serve the UI
  POST /api/chat    â†' SSE streaming chat
  POST /api/tts     â†' text-to-speech (MP3)
  GET  /api/status  â†' SUNI status (memory count, model, etc.)
"""
from __future__ import annotations
import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import edge_tts
import httpx
import secrets
from fastapi import FastAPI, Request, BackgroundTasks, Header, Depends, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

_limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])

from ..core.context import Context
from ..core.orchestrator import Orchestrator, activity_log
from ..models.ollama_agent import OllamaAgent
from ..models.claude_code_agent import ClaudeCodeAgent
from ..memory.manager import MemoryManager
from ..tools.registry import ToolRegistry
from ..tools import shell_tool, file_tool, claude_code_tool, claude_code_advanced, web_tool, email_tool, pdf_tool, document_tool, download_tool, kb_tool, skills_tool, contacts_tool, monitor_tool, task_tool, project_tool, database_tool, calendar_tool, articles_tool, image_tool
from ..skills import SkillStore
from ..tools.mcp_bridge import MCPBridge, CLAUDE_DESKTOP_CONFIG
from ..ingestion.watcher import watch
from ..ingestion.document_scanner import watch_documents, scanner_status
from ..memory.document_store import DocumentStore
from ..notifications.email_reader import watch as watch_inbox
from ..main import resolve_system_prompt
from ..whatsapp.handler import send_reply as wa_send, is_configured as wa_configured
from ..telegram.handler import (
    send_reply as tg_send, is_configured as tg_configured,
    poll_updates as tg_poll_updates, SECRET_TOKEN as _TG_SECRET,
)
from ..discord.handler import (
    send_reply as dc_send, is_configured as dc_configured,
    run_gateway as dc_run_gateway,
)
from ..slack.handler import (
    send_reply as sk_send, is_configured as sk_configured,
    run_gateway as sk_run_gateway,
)
from urllib.parse import quote
from .. import config as suni_config
from .. import auth as _auth
from .. import oidc as _oidc
from .. import audit as _audit
from .. import rbac as _rbac
from .. import backup as _backup
from .. import conversations as _conversations
from .. import user_settings as _user_settings
from ..logger import get_logger

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TTS_VOICE   = os.environ.get("SUNI_TTS_VOICE", "en-GB-SoniaNeural")

# edge-tts voices we expose in the UI. Every entry MUST be a real edge-tts
# ShortName — an invalid name yields NoAudioReceived (silent 503), and picking a
# voice whose language differs from the reply reads the text with a foreign
# accent (e.g. an English voice speaking Portuguese). Keep this the single source
# of truth for both /api/tts/voices and server-side voice validation.
EDGE_TTS_VOICES = [
    # English
    "en-GB-SoniaNeural", "en-GB-RyanNeural", "en-GB-LibbyNeural",
    "en-US-JennyNeural", "en-US-GuyNeural", "en-AU-NatashaNeural",
    "en-IE-EmilyNeural", "en-IN-NeerjaNeural",
    # European Portuguese
    "pt-PT-RaquelNeural", "pt-PT-DuarteNeural",
]
_EDGE_TTS_VOICE_SET = set(EDGE_TTS_VOICES)

# Language (BCP-47) → default edge-tts voice, so speech follows the reply
# language when the user hasn't pinned a valid voice of their own. Returns None
# for languages we have no dedicated voice for (English/unknown fall back to the
# global default). Match longest prefix first (pt-BR before pt).
_LANG_DEFAULT_VOICE = {
    "pt-pt": "pt-PT-RaquelNeural",
    "pt-br": "pt-BR-FranciscaNeural",
    "pt":    "pt-PT-RaquelNeural",
}


def _voice_for_lang(lang: str | None) -> str | None:
    """Best default voice for a BCP-47 language tag, or None if we have none."""
    l = (lang or "").strip().lower()
    if not l:
        return None
    for key in ("pt-pt", "pt-br"):     # specific region tags first
        if l.startswith(key):
            return _LANG_DEFAULT_VOICE[key]
    if l.startswith("pt"):
        return _LANG_DEFAULT_VOICE["pt"]
    return None
from ..model_inventory import resolve_model

# Whether the Claude Code CLI is installed. Cached with a short TTL because
# /api/status is polled by the admin dashboard and each check was two full PATH
# scans — ~70ms per request on a normal Windows PATH, for a fact that changes
# only when someone installs or removes a CLI. The TTL keeps it noticing an
# install without a restart.
_CC_TTL_S = 60.0
_cc_cache: tuple[float, bool] = (0.0, False)


def _claude_code_present() -> bool:
    global _cc_cache
    now = time.monotonic()
    stamp, value = _cc_cache
    if now - stamp > _CC_TTL_S:
        value = bool(shutil.which("claude") or shutil.which("claude.cmd"))
        _cc_cache = (now, value)
    return value

# Config → SUNI_MODEL env → best installed at the core tier. Read at import for
# the module-level constant, and re-resolved in create_app() so a model chosen
# in the admin panel takes effect on the next start rather than being discarded.
SUNI_MODEL  = resolve_model()
EMBED_MODEL = os.environ.get("SUNI_EMBED_MODEL", "qwen2.5:1.5b")
MEMORY_PATH = os.environ.get("SUNI_MEMORY_PATH", "memory/suni_memory.json")
UI_FILE     = Path(__file__).parent / "ui.html"
ADMIN_FILE  = Path(__file__).parent / "admin.html"
ARCH_FILE   = Path(__file__).parent / "architecture.html"
LOGIN_FILE  = Path(__file__).parent / "login.html"
SETUP_FILE  = Path(__file__).parent / "setup.html"
CHAT_FILE   = Path(__file__).parent / "chat.html"
FACE_FILE   = Path(__file__).parent / "face.html"
I18N_FILE   = Path(__file__).parent / "i18n.js"
_START_TIME      = time.time()
_METRICS_HISTORY: deque = deque(maxlen=120)
_METRICS_INTERVAL = 30

# â"€â"€ API Authentication â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
_MAX_CHAT_CHARS = 12_000   # M2: max message length
_MAX_BODY_BYTES = 65_536   # M2: 64 KB request body limit

# ── Page-level auth (cookie-based) ─────────────────────────────────────────────
_AUTH_COOKIE = "suni_auth"   # httpOnly cookie holding the refresh token (30 days)

def _set_auth_cookie(response: Response, refresh_token: str) -> None:
    """Attach the refresh token as an httpOnly session cookie."""
    response.set_cookie(
        key=_AUTH_COOKIE,
        value=refresh_token,
        httponly=True,
        samesite="lax",
        max_age=30 * 24 * 3600,
        path="/",
    )

def _check_page_auth(request: "Request", admin_required: bool = False):
    """
    Server-side guard for HTML routes.
    Returns (user_dict, None) on success, or (None, RedirectResponse) when
    the user must be redirected (unauthenticated or insufficient role).
    """
    token = request.cookies.get(_AUTH_COOKIE)
    if not token:
        return None, RedirectResponse(f"/login?next={request.url.path}", status_code=302)
    uid = _auth.verify_refresh_token(token)
    if not uid:
        return None, RedirectResponse(f"/login?next={request.url.path}", status_code=302)
    user = _auth.get_user(uid)
    if not user or not user.get("active", 1):
        return None, RedirectResponse(f"/login?next={request.url.path}", status_code=302)
    if admin_required and user.get("role") != "admin":
        return None, RedirectResponse("/", status_code=302)
    return user, None

def _get_or_create_token() -> str:
    """Load persisted API token or generate a new one."""
    p = Path("memory/api_key.txt")
    p.parent.mkdir(exist_ok=True)
    if p.exists():
        t = p.read_text().strip()
        if len(t) >= 32:
            return t
    t = secrets.token_urlsafe(32)
    p.write_text(t)
    return t

_API_TOKEN: str = _get_or_create_token()

_SERVICE_USER = {"id": "service", "username": "service", "role": "admin", "active": 1}

def get_current_user(
    authorization: str | None = Header(default=None),
    x_api_key:    str | None = Header(default=None, alias="x-api-key"),
) -> dict:
    """
    FastAPI dependency — accepts:
      1. JWT Bearer token (browser users via login page)
      2. Static API token from memory/api_key.txt (service accounts / MCP)
      3. Per-user API token generated in admin panel
    Returns user dict with id, username, role.
    """
    raw_token = None
    if authorization and authorization.lower().startswith("bearer "):
        raw_token = authorization[7:].strip()
    raw_token = raw_token or x_api_key or ""

    # Static service token (backward compat for MCP and scripts)
    if raw_token == _API_TOKEN:
        return _SERVICE_USER

    # JWT (browser users)
    user = _auth.verify_token(raw_token)
    if user:
        return user

    # Per-user API token (programmatic access)
    user = _auth.verify_api_token(raw_token)
    if user:
        return user

    raise HTTPException(status_code=401, detail="Not authenticated")


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# Backward-compat alias used in existing Depends() calls
def _check_token(authorization: str | None = Header(default=None),
                 x_api_key:    str | None = Header(default=None, alias="x-api-key")) -> None:
    get_current_user(authorization, x_api_key)

# ---------------------------------------------------------------------------
# Orchestrator setup (shared across requests)
# ---------------------------------------------------------------------------

def _build_orchestrator(
    memory: MemoryManager,
    skill_store: SkillStore | None = None,
    tier_map: dict | None = None,       # {tier: ModelInfo} from model_inventory
) -> tuple["Orchestrator", "ToolRegistry"]:
    registry = ToolRegistry()
    registry.register(shell_tool.SCHEMA, shell_tool.handler)
    registry.register(file_tool.READ_SCHEMA, file_tool.read_file)
    registry.register(file_tool.WRITE_SCHEMA, file_tool.write_file)
    registry.register(file_tool.LIST_SCHEMA, file_tool.list_files)
    registry.register(claude_code_tool.SCHEMA, claude_code_tool.handler)
    registry.register(claude_code_advanced.TASK_SCHEMA, claude_code_advanced.task_handler)
    registry.register(claude_code_advanced.AGENT_SCHEMA, claude_code_advanced.agent_handler)
    registry.register(claude_code_advanced.INIT_SCHEMA, claude_code_advanced.init_handler)
    registry.register(claude_code_advanced.SCHEDULE_SCHEMA, claude_code_advanced.schedule_handler)
    registry.register(claude_code_advanced.ADVISOR_SCHEMA, claude_code_advanced.advisor_handler)
    from ..tools import agent_tool, schedule_tool, network_tool, memory_tool
    # network_tool and memory_tool were registered in main.py's CLI registry and
    # NOT here, so the web UI silently lacked ping_host and memory_save/search.
    # run() already calls memory_tool.bind() every request, which does nothing
    # if the tools are not registered — a binding kept alive for tools that were
    # never exposed.
    registry.register(network_tool.SCHEMA, network_tool.handler)
    registry.register(memory_tool.SAVE_SCHEMA,   memory_tool.save_handler)
    registry.register(memory_tool.SEARCH_SCHEMA, memory_tool.search_handler)
    registry.register(memory_tool.DELETE_SCHEMA, memory_tool.delete_handler)
    registry.register(memory_tool.LIST_SCHEMA,   memory_tool.list_handler)
    registry.register(agent_tool.SCHEMA, agent_tool.handler)
    registry.register(agent_tool.LIST_SCHEMA, agent_tool.list_handler)
    registry.register(schedule_tool.SCHEMA, schedule_tool.handler)
    registry.register(schedule_tool.LIST_SCHEMA, schedule_tool.list_handler)
    registry.register(schedule_tool.DELETE_SCHEMA, schedule_tool.delete_handler)
    registry.register(web_tool.SEARCH_SCHEMA, web_tool.search)
    registry.register(web_tool.FETCH_SCHEMA,  web_tool.fetch_url)
    registry.register(email_tool.SCHEMA, email_tool.handler)
    registry.register(email_tool.LIST_SCHEMA, email_tool.list_handler)
    registry.register(email_tool.READ_SCHEMA, email_tool.read_handler)
    registry.register(pdf_tool.SCHEMA, pdf_tool.handler)
    registry.register(document_tool.SCHEMA, document_tool.handler)
    registry.register(image_tool.SCHEMA, image_tool.handler)
    registry.register(download_tool.SCHEMA, download_tool.handler)
    registry.register(kb_tool.SCHEMA, kb_tool.search)
    registry.register(articles_tool.RECENT_SCHEMA, articles_tool.get_recent_articles)
    registry.register(articles_tool.SEARCH_SCHEMA, articles_tool.search_articles)
    registry.register(articles_tool.STATS_SCHEMA,  articles_tool.get_article_stats)
    registry.register(skills_tool.LIST_SCHEMA,   skills_tool.list_handler)
    registry.register(skills_tool.VIEW_SCHEMA,   skills_tool.view_handler)
    registry.register(skills_tool.SAVE_SCHEMA,   skills_tool.save_handler)
    registry.register(skills_tool.DELETE_SCHEMA, skills_tool.delete_handler)
    registry.register(contacts_tool.SEARCH_SCHEMA, contacts_tool.search_handler)
    registry.register(contacts_tool.ADD_SCHEMA,    contacts_tool.add_handler)
    registry.register(contacts_tool.UPDATE_SCHEMA, contacts_tool.update_handler)
    registry.register(contacts_tool.NOTE_SCHEMA,   contacts_tool.note_handler)
    registry.register(monitor_tool.ADD_TOPIC_SCHEMA, monitor_tool.add_topic_handler)
    registry.register(monitor_tool.ADD_FEED_SCHEMA,  monitor_tool.add_feed_handler)
    registry.register(monitor_tool.LIST_SCHEMA,      monitor_tool.list_handler)
    registry.register(monitor_tool.REMOVE_SCHEMA,    monitor_tool.remove_handler)
    registry.register(monitor_tool.ALERTS_SCHEMA,    monitor_tool.alerts_handler)
    registry.register(task_tool.LIST_SCHEMA,   task_tool.list_handler)
    registry.register(task_tool.STATUS_SCHEMA, task_tool.status_handler)
    registry.register(project_tool.CREATE_SCHEMA,  project_tool.create_handler)
    registry.register(project_tool.LIST_SCHEMA,    project_tool.list_handler)
    registry.register(project_tool.GET_SCHEMA,     project_tool.get_handler)
    registry.register(project_tool.LOG_SCHEMA,     project_tool.log_handler)
    registry.register(project_tool.UPDATE_SCHEMA,  project_tool.update_handler)
    registry.register(project_tool.SHARE_SCHEMA,   project_tool.share_handler)
    registry.register(project_tool.MEMBERS_SCHEMA, project_tool.members_handler)
    registry.register(database_tool.SCHEMA_SCHEMA,  database_tool.schema_handler)
    registry.register(database_tool.QUERY_SCHEMA,   database_tool.query_handler)
    registry.register(database_tool.EXECUTE_SCHEMA, database_tool.execute_handler)
    registry.register(calendar_tool.LIST_EVENTS_SCHEMA,   calendar_tool.list_events_handler)
    registry.register(calendar_tool.CREATE_EVENT_SCHEMA,  calendar_tool.create_event_handler)
    registry.register(calendar_tool.FREE_SLOTS_SCHEMA,    calendar_tool.free_slots_handler)
    registry.register(calendar_tool.DELETE_EVENT_SCHEMA,  calendar_tool.delete_event_handler)
    registry.register(calendar_tool.CALENDARS_SCHEMA,     calendar_tool.list_calendars_handler)

    # Article tools read the GLOBAL store directly — articles are global SUNI
    # content and live only in `memory`, never in the per-user stores that
    # _safe_run binds for web requests.
    articles_tool.bind_global(memory)

    suni, tier_agents = _make_backend_agents(tier_map)

    orchestrator = Orchestrator(
        primary=suni, registry=registry, memory=memory,
        skill_store=skill_store, tier_agents=tier_agents,
    )
    orchestrator._last_tier_map = tier_map   # for the runtime backend switch
    # invoke_agent runs a nested turn, so it needs the orchestrator. Bound here
    # rather than imported inside the tool, which would be a circular import and
    # would also leave the tool silently answering "unavailable" if the wiring
    # were ever missed.
    agent_tool.bind(orchestrator)
    return orchestrator, registry


def _make_backend_agents(tier_map):
    """Build (primary, tier_agents) for the ACTIVE backend (Ollama or vLLM).
    Shared by startup and the runtime backend switch so both stay in sync.

    vLLM mode: one server serves one model, so all local tiers collapse to a
    single vLLM agent (local-tier escalation becomes a no-op; T5/Claude Code
    stays the real escalation target). Ollama mode: per-tier agents as before.
    """
    from ..system_profile import DEFAULT_TIER
    from ..models import factory as _factory
    backend = _factory.active_backend()
    tier_agents: dict = {}

    if backend == "vllm":
        suni = _factory.make_agent("suni_vllm", model=SUNI_MODEL,
                                   system_prompt=resolve_system_prompt(), backend="vllm")
        tier_agents[DEFAULT_TIER] = suni
        _log.info("[BACKEND] vLLM active → %s @ %s",
                  suni.model, getattr(suni, "host", "?"))
    else:
        if tier_map:
            for tier, info in tier_map.items():
                tier_agents[tier] = _factory.make_agent(
                    f"suni_t{tier}", model=info.name,
                    system_prompt=resolve_system_prompt(), host=info.endpoint, backend="ollama")
                _log.info("[TIER] tier %d → %s @ %s", tier, info.name, info.endpoint)
        primary_model = (tier_agents[DEFAULT_TIER].model
                         if DEFAULT_TIER in tier_agents else SUNI_MODEL)
        suni = tier_agents.get(DEFAULT_TIER) or _factory.make_agent(
            "suni", model=primary_model, system_prompt=resolve_system_prompt(), backend="ollama")

    # Register Claude Code as T5 if the CLI is available (both backends).
    import shutil as _shutil
    from ..core.model_tier import CLAUDE_CODE_TIER as _CC_TIER
    if _shutil.which("claude") or _shutil.which("claude.cmd") or _shutil.which("claude.CMD"):
        tier_agents[_CC_TIER] = ClaudeCodeAgent()
        _log.info("[TIER] tier %d → Claude Code (direct)", _CC_TIER)

    # Opt-in model-chain routing: the first ENABLED chain entry overrides the
    # primary model/provider (escalation to Claude Code is unchanged). Flag-gated
    # and fail-safe — any build error keeps the default primary, so a bad chain
    # entry can never break the working router. NOTE: force_claude_code
    # short-circuits the tier ladder, so this only bites with force_claude_code=False.
    if bool(suni_config.get("model_chain_routing")):
        _chain = [e for e in (suni_config.get("model_chain") or [])
                  if isinstance(e, dict) and e.get("enabled", True)]
        if _chain:
            _e = _chain[0]
            try:
                _p = _factory.make_provider_agent(
                    "suni_chain", _e.get("provider", "ollama"), _e.get("model", ""),
                    _e.get("base_url", ""), _e.get("api_key", ""), resolve_system_prompt())
                suni = _p
                tier_agents[DEFAULT_TIER] = _p
                _log.info("[CHAIN] primary → %s / %s (model-chain routing on)",
                          _e.get("provider"), _e.get("model") or "(default)")
            except Exception as _cx:
                _log.warning("[CHAIN] primary build failed — keeping default: %s", _cx)

    return suni, tier_agents


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    app = FastAPI(title="SUNI", docs_url=None, redoc_url=None)
    app.state.limiter = _limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    # CORS — the UI is served same-origin, so it needs no cross-origin grant.
    # Restrict to localhost (the dev origin) + an explicit SUNI_PUBLIC_URL rather
    # than the previous wildcard, which let any site issue cross-origin API calls.
    _cors_origins = [
        "https://localhost:8765", "https://127.0.0.1:8765",
        "http://localhost:8765",  "http://127.0.0.1:8765",
    ]
    _pub_url = os.environ.get("SUNI_PUBLIC_URL", "").strip().rstrip("/")
    if _pub_url:
        _cors_origins.append(_pub_url)
    app.add_middleware(CORSMiddleware, allow_origins=_cors_origins,
                       allow_methods=["*"], allow_headers=["*"])

    # M2 — request body size limit. Multipart upload routes are exempt (they
    # carry files far larger than 64 KB) and enforce their own per-endpoint limits.
    _LARGE_UPLOAD_PATHS = ("/api/files/upload", "/api/stt/transcribe")

    @app.middleware("http")
    async def _limit_body(request: Request, call_next):
        if not request.url.path.startswith(_LARGE_UPLOAD_PATHS):
            cl = request.headers.get("content-length")
            if cl and int(cl) > _MAX_BODY_BYTES:
                return JSONResponse({"error": "Request body too large (max 64 KB)"}, status_code=413)
        return await call_next(request)

    doc_store   = DocumentStore(
        index_path="memory/doc_index.faiss",
        meta_path="memory/doc_meta.json",
    )
    kb_tool.set_doc_store(doc_store)
    memory      = MemoryManager(store_path=MEMORY_PATH, embed_model=EMBED_MODEL,
                                doc_store=doc_store)
    skill_store = SkillStore()
    skills_tool.set_store(skill_store)

    # Build model inventory (async; runs in background, orchestrator rebuilt when ready)
    from .. import model_inventory as _inv
    from ..model_inventory import ModelInfo as _ModelInfo

    _inventory: list[_ModelInfo] = _inv.load_cached()  # instant; may be empty on first run
    _tier_map: dict[int, _ModelInfo] = _inv.tier_map(_inventory) if _inventory else {}

    orchestrator, registry = _build_orchestrator(memory, skill_store=skill_store,
                                                  tier_map=_tier_map)
    bridge      = MCPBridge(registry)

    # M1 — per-session contexts keyed by session ID (UUID from client cookie/header)
    _sessions: dict = {}   # sid -> (Context, asyncio.Lock, last_used_ts)
    _SESSION_TTL = 7200  # 2 hours of inactivity before expiry

    def _get_context(session_id: str, default_mode: str = "assistant"):
        """Return (Context, Lock, mode) for the given session."""
        now = time.time()
        stale = [k for k, v in _sessions.items() if now - v[-1] > _SESSION_TTL]
        for k in stale:
            del _sessions[k]
        if session_id not in _sessions:
            _sessions[session_id] = (Context(), asyncio.Lock(), default_mode, now)
        ctx, lock, mode, _ = _sessions[session_id]
        _sessions[session_id] = (ctx, lock, mode, now)
        return ctx, lock, mode

    def _set_session_mode(session_id: str, mode: str):
        if session_id in _sessions:
            ctx, lock, _, ts = _sessions[session_id]
            _sessions[session_id] = (ctx, lock, mode, ts)

    stop_event   = asyncio.Event()
    wa_contexts: dict[str, Context] = {}   # per-WhatsApp-user contexts
    tg_contexts: dict[int, Context] = {}   # per-Telegram-chat contexts
    dc_contexts: dict[str, Context] = {}   # per-Discord-channel contexts
    sk_contexts: dict[str, Context] = {}   # per-Slack-channel contexts

    async def _handle_sk(channel_id, text: str, is_dm: bool = False) -> None:
        """Shared inbound handler for Slack messages (Socket Mode).

        SECURITY GATE (fail closed): `slack_allowed_channels` is the only
        authorization boundary. Unknown channel → ignored (a DM gets its channel
        id so the owner can allow-list it; a public channel stays silent to avoid
        spam). Allowed channels run at role "standard" (least-privilege)."""
        allowed = {str(c).strip() for c in (suni_config.get("slack_allowed_channels") or [])}
        if str(channel_id) not in allowed:
            _log.info("[SLACK] message from non-allow-listed channel %s — ignored", channel_id)
            if is_dm:
                await sk_send(channel_id,
                              f"This SUNI is private. Your channel id is {channel_id} — "
                              f"ask the owner to add it to the Slack allow-list.")
            return
        if channel_id not in sk_contexts:
            sk_contexts[channel_id] = Context()
        chan_ctx = sk_contexts[channel_id]
        # Per-channel memory isolation: a filesystem-safe pseudo user-id gives this
        # Slack channel its own MemoryManager, so different channels/DMs don't share
        # episodic memory (a shared room shares memory; separate DMs stay private).
        _uid = f"slack_{channel_id}"
        try:
            response = await orchestrator._safe_run(
                text, chan_ctx, memory_override=_get_user_memory(_uid),
                user_role="standard", user_id=_uid)
            await sk_send(channel_id, response)
        except Exception as e:
            await sk_send(channel_id, f"I encountered a difficulty: {e}")

    async def _handle_dc(channel_id, text: str, is_dm: bool = False) -> None:
        """Shared inbound handler for Discord messages (gateway).

        SECURITY GATE (fail closed): `discord_allowed_channels` is the only
        authorization boundary. Unknown channel → ignored. In a DM (no spam
        risk) we reply with the channel id so the owner can allow-list it; in a
        guild we stay silent (replying in every channel would be spam) and just
        log the id. Allowed channels run at role "standard" (least-privilege)."""
        allowed = {str(c).strip() for c in (suni_config.get("discord_allowed_channels") or [])}
        if str(channel_id) not in allowed:
            _log.info("[DISCORD] message from non-allow-listed channel %s — ignored", channel_id)
            if is_dm:
                await dc_send(channel_id,
                              f"This SUNI is private. Your channel id is {channel_id} — "
                              f"ask the owner to add it to the Discord allow-list.")
            return
        if channel_id not in dc_contexts:
            dc_contexts[channel_id] = Context()
        chan_ctx = dc_contexts[channel_id]
        _uid = f"discord_{channel_id}"
        try:
            response = await orchestrator._safe_run(
                text, chan_ctx, memory_override=_get_user_memory(_uid),
                user_role="standard", user_id=_uid)
            await dc_send(channel_id, response)
        except Exception as e:
            await dc_send(channel_id, f"I encountered a difficulty: {e}")

    async def _handle_tg(chat_id, text: str) -> None:
        """Shared inbound handler for BOTH the Telegram webhook and long-poll.

        SECURITY GATE (fail closed): `telegram_allowed_chats` is the only
        authorization boundary for the pollling transport (no per-request
        secret). Empty/unknown chat → we reply with the chat id so the owner can
        allow-list it, and DO NOT run the model. Allowed chats run at role
        "standard" (least-privilege — no shell / claude_task / T5-direct)."""
        allowed = {str(c).strip() for c in (suni_config.get("telegram_allowed_chats") or [])}
        if str(chat_id) not in allowed:
            _log.warning("[TELEGRAM] message from non-allow-listed chat %s — refused", chat_id)
            await tg_send(chat_id,
                          f"This SUNI is private. Your chat id is {chat_id} — ask the "
                          f"owner to add it to the Telegram allow-list to enable access.")
            return
        if chat_id not in tg_contexts:
            tg_contexts[chat_id] = Context()
        chat_ctx = tg_contexts[chat_id]
        _uid = f"telegram_{chat_id}"
        try:
            response = await orchestrator._safe_run(
                text, chat_ctx, memory_override=_get_user_memory(_uid),
                user_role="standard", user_id=_uid)
            await tg_send(chat_id, response)
        except Exception as e:
            await tg_send(chat_id, f"I encountered a difficulty: {e}")

    # Collective episodic memory (company-level facts shared across all users)
    from ..memory.store import MemoryStore as _MemStore
    _collective_store = _MemStore("memory/collective_memory.json")

    # Per-user MemoryManagers keyed by user_id
    _user_memories: dict[str, MemoryManager] = {}

    def _get_user_memory(user_id: str) -> MemoryManager:
        if user_id not in _user_memories:
            user_path = f"memory/users/{user_id}/suni_memory.json"
            _user_memories[user_id] = MemoryManager(
                store_path=user_path, embed_model=EMBED_MODEL, doc_store=doc_store,
                collective_store=_collective_store,
            )
        return _user_memories[user_id]

    def _ingest_target():
        """Which store the session watcher writes into, re-asked every cycle.

        Locally-ingested Claude Code transcripts are somebody's personal data,
        but the ingest path has no authenticated user. With an owner named in
        the config they go to that user's own store, where erasure and subject
        access already work; with none they go to the shared store, which is
        the historical behaviour and cannot be attributed to anyone.

        Fails SAFE, never silently: an owner that has been deleted or disabled
        falls back to the shared store and says so, because dropping the
        ingest instead would lose the transcripts entirely.
        """
        try:
            owner = str(suni_config.load().get("session_ingest_owner", "") or "").strip()
        except Exception as exc:      # noqa: BLE001
            _log.warning("[INGEST] could not read session_ingest_owner (%s) — "
                         "using the shared store", exc)
            return memory
        if not owner:
            return memory
        user = _auth.get_user(owner)
        if not user or not user.get("active", True):
            _log.warning("[INGEST] session_ingest_owner %r is unknown or inactive "
                         "— falling back to the shared store", owner)
            return memory
        return _get_user_memory(owner)

    # ── Channel supervisor — live start/stop without a restart ─────────────
    # Each channel runs as a background task with its OWN stop Event, so it can
    # be started/stopped/restarted from the admin panel (POST /api/config)
    # instead of only at boot. run_fn(dispatch, event) is the channel's loop.
    _channel_tasks: dict[str, tuple] = {}   # name -> (task, asyncio.Event)
    _CHANNELS = {
        # name:      (run_fn,          dispatch,    configured_fn, trigger keys that force a (re)start)
        "telegram": (tg_poll_updates, _handle_tg, tg_configured, {"telegram_enabled", "telegram_bot_token"}),
        "discord":  (dc_run_gateway,  _handle_dc, dc_configured, {"discord_enabled",  "discord_bot_token"}),
        "slack":    (sk_run_gateway,  _handle_sk, sk_configured, {"slack_enabled", "slack_app_token", "slack_bot_token"}),
    }

    async def _stop_channel(name: str) -> None:
        entry = _channel_tasks.pop(name, None)
        if not entry:
            return
        task, ev = entry
        ev.set()                              # graceful — the loop exits on its event
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=30)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            task.cancel()                     # backstop if it didn't stop in time
        _log.info("[CHANNEL] %s stopped", name)

    async def _start_channel(name: str) -> None:
        await _stop_channel(name)             # ensure only one instance
        run_fn, dispatch, _configured, _ = _CHANNELS[name]
        ev = asyncio.Event()
        _channel_tasks[name] = (asyncio.create_task(run_fn(dispatch, ev)), ev)
        _log.info("[CHANNEL] %s started", name)

    async def _reconcile_channel(name: str) -> None:
        """Bring a channel in line with config: start if it should run and
        isn't, stop if it shouldn't, restart if running (picks up a new token)."""
        run_fn, _dispatch, configured_fn, _keys = _CHANNELS[name]
        should = bool(suni_config.get(f"{name}_enabled")) and configured_fn()
        running = name in _channel_tasks
        if should:
            await _start_channel(name)        # (re)start — covers token changes too
        elif running:
            await _stop_channel(name)


    # ── Scheduled invocations ──────────────────────────────────────────────
    async def _schedule_runner(stop_event):
        """Replay due prompts through the orchestrator, as their owner.

        Deliberately runs each entry under the owner's role AS IT IS NOW, looked
        up fresh every fire. A snapshot taken at creation would let a schedule
        preserve permissions its owner has since lost.

        Delivery is performed HERE, not by the model. A scheduled run has no
        approver, so send_email would either be denied (making delivery
        impossible) or allowed unattended (letting a model choose recipients with
        nobody watching). The destination was fixed by a human when the schedule
        was created.
        """
        from .. import schedules as _sched
        from .. import agents as _agents
        from ..notifications import email_notify as _mail
        # Wait before the first pass. The first due() call creates schedules.db,
        # and doing that while the app is still warming up put a ~800ms outlier
        # on the very first request. Nothing is lost: an entry due at startup
        # simply fires one tick later.
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
        while not stop_event.is_set():
            try:
                # Audit retention rides this loop rather than owning a timer:
                # it is a once-a-day job and apply_retention() enforces that
                # itself, so the 30s tick just gives it a chance to run. It
                # never raises, so a purge failure cannot stop schedules.
                _ret = _audit.apply_retention()
                if _ret["ran"]:
                    _log.info("[AUDIT] retention: deleted %d rows older than %d days",
                              _ret["deleted"], _ret["days"])
                elif _ret["reason"].startswith(("purge failed", "could not read")):
                    _log.warning("[AUDIT] retention: %s", _ret["reason"])

                for s in _sched.due():
                    owner = _auth.get_user(s["owner_id"])
                    if not owner or not owner.get("active", True):
                        _sched.mark_ran(s["id"], "skipped: owner inactive", s["cadence"])
                        continue
                    role = owner.get("role", "standard")
                    profile = None
                    if s["agent_slug"]:
                        visible = {a["slug"] for a in _agents.list_for_user(owner["id"], role)}
                        if s["agent_slug"] in visible:
                            profile = _agents.get(s["agent_slug"])
                            if profile:
                                profile["_username"] = owner.get("username", "")
                        else:
                            _sched.mark_ran(s["id"], "skipped: agent not available to owner",
                                            s["cadence"])
                            continue
                    status = "ok"
                    from .. import updater as _upd
                    _upd.mark_busy(+1)
                    try:
                        from ..core.context import Context as _Ctx
                        ctx = _Ctx()
                        answer = await orchestrator._safe_run(
                            s["prompt"], ctx,
                            memory_override=_get_user_memory(owner["id"]),
                            user_role=role,
                            user_id=owner["id"],
                            agent_profile=profile,
                        )
                        d = s.get("delivery") or {}
                        if d.get("type") == "email" and d.get("to"):
                            _mail.send_email(d["to"], f"SUNI — {s['name']}", answer,
                                             user_id=owner["id"])
                    except Exception as exc:
                        status = f"error: {exc}"
                        _log.error("[SCHEDULE] %s failed: %s", s["id"], exc, exc_info=True)
                    _upd.mark_busy(-1)
                    _sched.mark_ran(s["id"], status, s["cadence"])
                    _audit.log_event(owner["id"], owner.get("username", ""),
                                     "schedule.ran",
                                     detail=f"{s['name']} -> {status}"[:100],
                                     target_id=s["id"], agent_slug=s["agent_slug"])
            except Exception as exc:
                _log.error("[SCHEDULE] runner loop error: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    @app.on_event("startup")
    async def _startup():
        nonlocal orchestrator, registry, _tier_map
        # Initialise auth, audit, and conversations databases
        _auth.init_db()
        _audit.init_db()
        _conversations.init_db()
        # Pre-warm nomic-embed-text so the first user request doesn't pay model load time
        try:
            from ..memory.manager import embed_nomic as _warm_nomic
            await _warm_nomic("warmup")
            _log.info("[STARTUP] nomic-embed-text pre-warmed")
        except Exception as _e:
            _log.warning("[STARTUP] nomic pre-warm failed: %s", _e)
        # Scan Ollama endpoints for available models and rebuild orchestrator with tier agents
        try:
            fresh_inv = await _inv.get_inventory(force_refresh=True)
            if fresh_inv:
                _tier_map = _inv.tier_map(fresh_inv)
                orchestrator, registry = _build_orchestrator(
                    memory, skill_store=skill_store, tier_map=_tier_map
                )
                _log.info("[STARTUP] Model inventory: %d models across %d tiers",
                          len(fresh_inv), len(_tier_map))
                # SUNI_MODEL was resolved at import, before any scan existed —
                # so on a fresh install the "use what is installed" fallback had
                # nothing to look at. Re-resolve now that we know.
                global SUNI_MODEL
                if not SUNI_MODEL:
                    SUNI_MODEL = _inv.resolve_model(fresh_inv)
        except Exception as _e:
            _log.warning("[STARTUP] Model inventory scan failed: %s", _e)
        _log.info("[STARTUP] SUNI started — model=%s port=8765",
                  SUNI_MODEL or "(none configured — set one in the admin panel)")
        # Connect to external MCP servers
        await bridge.start()
        asyncio.create_task(watch(memory, stop_event, resolve=_ingest_target))
        asyncio.create_task(watch_inbox(stop_event))
        asyncio.create_task(_metrics_collector(stop_event))
        # Backend circuit-breaker recovery probe (Ollama liveness)
        from ..models import health as _bhealth
        asyncio.create_task(_bhealth.start_monitor(stop_event))
        asyncio.create_task(watch_documents(
            doc_store=doc_store,
            embed_fn=memory.embed_batch_sync,
            stop_event=stop_event,
            get_paths=lambda: suni_config.get("doc_paths", []),
            get_interval=lambda: suni_config.get("doc_scan_interval", 600),
        ))
        asyncio.create_task(_schedule_runner(stop_event))
        # News/web monitor
        _monitor_interval = suni_config.get("monitor_interval", 3600)
        from .. import monitor as _monitor_mod
        asyncio.create_task(_monitor_mod.start_monitor(interval=_monitor_interval))
        # Telegram long-poll gateway (works with no public URL). Only starts when
        # enabled AND a bot token is configured; mutually exclusive with the
        # /telegram webhook (poll_updates deletes any active webhook first).
        # Channels (Telegram long-poll / Discord + Slack gateways) — all connect
        # out, no public URL. Started via the supervisor so they can be toggled
        # live from the admin panel without a restart.
        for _ch in _CHANNELS:
            try:
                await _reconcile_channel(_ch)
            except Exception as _e:
                _log.warning("[STARTUP] channel %s start failed: %s", _ch, _e)
        # Daily briefing scheduler
        from .. import briefing as _briefing_mod
        asyncio.create_task(_briefing_mod.start_briefing_scheduler())
        # Weekly memory consolidation scheduler (interval from config)
        from ..memory import consolidator as _consolidator_mod
        asyncio.create_task(_consolidator_mod.start_consolidation_scheduler(
            get_user_managers_fn=lambda: dict(_user_memories),
            model=SUNI_MODEL,
            interval_days=suni_config.get("memory_consolidate_interval_days", 7),
        ))

    @app.on_event("shutdown")
    async def _shutdown():
        _log.info("[SHUTDOWN] SUNI stopping")
        stop_event.set()
        # Stop channel gateways (they use their own per-channel stop events).
        for _ch in list(_channel_tasks):
            try:
                await _stop_channel(_ch)
            except Exception:
                pass
        from .. import monitor as _monitor_mod
        _monitor_mod.stop_monitor()
        from .. import briefing as _briefing_mod
        _briefing_mod.stop_briefing_scheduler()
        from ..memory import consolidator as _consolidator_mod
        _consolidator_mod.stop_consolidation_scheduler()
        await bridge.stop()

    # -----------------------------------------------------------------------
    # Routes
    # -----------------------------------------------------------------------

    def _inject_token(html: str) -> str:
        """Embed the API token into served HTML so JS can use it for API calls."""
        return html.replace("__SUNI_TOKEN__", _API_TOKEN)

    def _inject_jwt(html: str, user: dict) -> str:
        """Embed a fresh access token so localStorage is always populated on page load."""
        return html.replace("__SUNI_JWT__", _auth.create_access_token(user))

    @app.get("/")
    async def index(request: Request):
        user, redir = _check_page_auth(request)
        if redir: return redir
        # A fresh instance has an admin account but no model, language or
        # documents chosen. Send the admin through the wizard once. Only the
        # admin: other users cannot change instance settings, so bouncing them
        # to a wizard they cannot complete would just lock them out.
        if user.get("role") == "admin" and not suni_config.get("setup_completed", False):
            return RedirectResponse("/setup", status_code=302)
        html = _inject_token(UI_FILE.read_text(encoding="utf-8"))
        return HTMLResponse(_inject_jwt(html, user))

    # ── First-run wizard ────────────────────────────────────────────────────
    @app.get("/setup")
    async def setup_page(request: Request):
        user, redir = _check_page_auth(request, admin_required=True)
        if redir: return redir
        html = _inject_token(SETUP_FILE.read_text(encoding="utf-8"))
        return HTMLResponse(_inject_jwt(html, user))

    # ── Persona (animated head) page ─────────────────────────────────────────
    @app.get("/face")
    async def face_page(request: Request):
        user, redir = _check_page_auth(request)
        if redir: return redir
        html = _inject_token(FACE_FILE.read_text(encoding="utf-8"))
        return HTMLResponse(_inject_jwt(html, user))

    # ── Login page ──────────────────────────────────────────────────────────
    @app.get("/login")
    async def login_page(request: Request):
        # Already authenticated → redirect home
        token = request.cookies.get(_AUTH_COOKIE)
        if token:
            uid = _auth.verify_refresh_token(token)
            if uid and _auth.get_user(uid):
                return RedirectResponse("/", status_code=302)
        return HTMLResponse(LOGIN_FILE.read_text(encoding="utf-8"))

    # ── Auth endpoints ──────────────────────────────────────────────────────
    @app.post("/api/auth/setup")
    async def auth_setup(request: Request):
        """First-run only: create initial admin account."""
        if not _auth.is_first_run():
            raise HTTPException(400, "Setup already complete")
        body = await request.json()
        username = body.get("username", "").strip()
        password = body.get("password", "")
        if not username or len(password) < 8:
            raise HTTPException(400, "Username required and password must be >= 8 chars")
        user = _auth.create_user(username, password, role="admin")
        _log.info("[AUTH] Admin account created: %s", username)
        refresh = _auth.create_refresh_token(user["id"])
        resp = JSONResponse({
            "access_token": _auth.create_access_token(user),
            "refresh_token": refresh,
            "token_type": "bearer",
        })
        _set_auth_cookie(resp, refresh)
        return resp

    @app.post("/api/auth/login")
    @_limiter.limit("10/minute")
    async def auth_login(request: Request):
        """Login with username + password. Returns JWT and sets session cookie."""
        # SSO-only mode disables password login — except while zero users exist,
        # so the first admin can still be bootstrapped via the setup wizard.
        if not _oidc.public_status()["password_enabled"]:
            raise HTTPException(403, "Password login is disabled; use SSO.")
        from fastapi import Form
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        user = _auth.verify_password_login(username, password)
        if not user:
            _log.warning("[AUTH] Failed login for username: %s", username)
            raise HTTPException(401, "Invalid credentials")
        _log.info("[AUTH] Login: %s (role=%s)", user["username"], user["role"])
        refresh = _auth.create_refresh_token(user["id"])
        resp = JSONResponse({
            "access_token":  _auth.create_access_token(user),
            "refresh_token": refresh,
            "token_type":    "bearer",
            "role":          user["role"],
            "username":      user["username"],
        })
        _set_auth_cookie(resp, refresh)
        return resp

    @app.post("/api/auth/refresh")
    async def auth_refresh(request: Request):
        body  = await request.json()
        token = body.get("refresh_token", "")
        uid   = _auth.verify_refresh_token(token)
        if not uid:
            raise HTTPException(401, "Invalid or expired refresh token")
        user = _auth.get_user(uid)
        if not user or not user["active"]:
            raise HTTPException(401, "User not found or inactive")
        resp = JSONResponse({
            "access_token": _auth.create_access_token(user),
            "token_type":   "bearer",
        })
        _set_auth_cookie(resp, token)   # keep the cookie in sync with the client token
        return resp

    @app.post("/api/auth/logout")
    async def auth_logout():
        """Clear the session cookie. Frontend must also clear localStorage."""
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(_AUTH_COOKIE, path="/")
        return resp

    @app.get("/api/auth/me")
    async def auth_me(user: dict = Depends(get_current_user)):
        return JSONResponse({
            "id": user["id"], "username": user["username"],
            "role": user["role"], "active": user.get("active", 1),
        })

    @app.get("/api/auth/first-run")
    async def auth_first_run():
        return JSONResponse({"first_run": _auth.is_first_run()})

    @app.post("/api/setup/complete")
    async def setup_complete(request: Request, admin: dict = Depends(require_admin)):
        """Finish the first-run wizard.

        Accepts only the handful of settings the wizard offers — the admin
        panel is the place for everything else, and a wide-open write here
        would be a second, less careful config endpoint.

        Always marks setup_completed, including when the body is empty
        (the user skipped). Otherwise the wizard reappears on every visit,
        which is worse than respecting the choice.
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        allowed = {"model", "display_name", "tagline",
                   "response_language", "stt_language", "doc_paths"}
        patch: dict = {}
        for key in allowed:
            if key not in body:
                continue
            val = body[key]
            if key == "doc_paths":
                if isinstance(val, list):
                    patch[key] = [str(v).strip() for v in val if str(v).strip()]
                continue
            val = str(val).strip()
            if val:                      # blank = leave the existing value alone
                patch[key] = val

        cfg = suni_config.all()
        cfg.update(patch)
        cfg["setup_completed"] = True
        suni_config.save(cfg)
        _log.info("[SETUP] first-run wizard completed by %s (set: %s)",
                  admin["username"], ", ".join(sorted(patch)) or "nothing")
        return JSONResponse({"ok": True, "applied": sorted(patch)})

    @app.get("/api/auth/status")
    async def auth_status():
        """Public: what login methods the UI should offer (no secrets).

        Also carries the instance's display name and tagline so the login page
        can brand itself before anyone has authenticated. Both are operator-set
        display strings — nothing sensitive — and the login page is the one
        place that cannot read /api/config.
        """
        return JSONResponse({
            "first_run":    _auth.is_first_run(),
            "display_name": suni_config.get("display_name", "") or "SUNI",
            "tagline":      suni_config.get("tagline", ""),
            **_oidc.public_status(),
        })

    @app.get("/api/backend/health")
    async def backend_health(user: dict = Depends(get_current_user)):
        """Circuit-breaker state for the local model backend(s)."""
        from ..models import health as _bhealth
        return JSONResponse(_bhealth.status())

    # ── OIDC / SSO ──────────────────────────────────────────────────────────
    @app.get("/api/auth/oidc/authorize")
    async def oidc_authorize(request: Request, next: str = "/"):
        if not _oidc.enabled():
            raise HTTPException(404, "SSO is not configured.")
        try:
            url = await _oidc.build_authorize_url(str(request.base_url), next)
        except _oidc.OidcError as e:
            _log.warning("[OIDC] authorize failed: %s", e)
            return RedirectResponse(f"/login?error={quote(str(e))}", status_code=302)
        except Exception as e:
            _log.warning("[OIDC] authorize error: %s", e)
            return RedirectResponse("/login?error=sso_unavailable", status_code=302)
        return RedirectResponse(url, status_code=302)

    @app.get("/api/auth/oidc/callback")
    @_limiter.limit("10/minute")
    async def oidc_callback(request: Request, code: str = "", state: str = "",
                            error: str = ""):
        if error:
            return RedirectResponse(f"/login?error={quote(error)}", status_code=302)
        if not _oidc.enabled():
            raise HTTPException(404, "SSO is not configured.")
        try:
            user, next_url = await _oidc.handle_callback(code, state)
        except _oidc.OidcError as e:
            _log.warning("[OIDC] callback refused: %s", e)
            return RedirectResponse(f"/login?error={quote(str(e))}", status_code=302)
        except Exception as e:
            _log.warning("[OIDC] callback error: %s", e)
            return RedirectResponse("/login?error=sso_failed", status_code=302)
        _log.info("[OIDC] login: %s (role=%s)", user["username"], user["role"])
        refresh = _auth.create_refresh_token(user["id"])
        # Land on the app with the session cookie set; the page bootstraps its
        # access token from /api/auth/refresh using this cookie.
        resp = RedirectResponse(_oidc.safe_next(next_url), status_code=302)
        _set_auth_cookie(resp, refresh)
        return resp

    # ── Per-user settings ───────────────────────────────────────────────
    @app.get("/api/users/me/settings")
    async def get_my_settings(user: dict = Depends(get_current_user)):
        s = _user_settings.get(user["id"])
        # Never echo stored secrets, not even as ciphertext — expose only
        # whether one is set, so the UI can show "configured".
        for _sk in ("anthropic_api_key", "smtp_pass"):
            s[f"has_{_sk}"] = bool(str(s.get(_sk, "")).strip())
            s[_sk] = ""
        return JSONResponse(s)

    @app.put("/api/users/me/settings")
    async def update_my_settings(request: Request, user: dict = Depends(get_current_user)):
        body = await request.json()
        # Blank secret = "keep the stored value" (GET redacts it), so simply
        # saving the settings form never wipes a configured password/key.
        for _sk in ("anthropic_api_key", "smtp_pass"):
            if _sk in body and not str(body[_sk]).strip():
                del body[_sk]
        # Validate output_dir: must be a subdirectory of global_output_dir
        if "output_dir" in body:
            out_raw = (body.get("output_dir") or "").strip()
            if out_raw:
                global_base = suni_config.get("global_output_dir", "").strip()
                if not global_base:
                    raise HTTPException(400, "An output directory requires the admin to set a global output directory first")
                try:
                    out_p    = Path(out_raw).resolve()
                    global_p = Path(global_base).resolve()
                    out_p.relative_to(global_p)
                    body["output_dir"] = str(out_p)
                except ValueError:
                    raise HTTPException(400, f"output_dir must be inside the global output directory: {global_base}")
            else:
                body["output_dir"] = ""
        result = _user_settings.save(user["id"], body)
        _log.info("[SETTINGS] %s updated personal settings", user["username"])
        return JSONResponse(result)

    # ── Conversation mode ───────────────────────────────────────────────
    @app.post("/api/session/mode")
    async def set_mode(request: Request, user: dict = Depends(get_current_user)):
        body = await request.json()
        mode       = body.get("mode", "assistant")
        session_id = body.get("session_id", user["id"])
        role       = user.get("role", "standard")
        if not _rbac.can_use_mode(role, mode):
            raise HTTPException(403, f"Role '{role}' cannot use mode '{mode}'")
        _set_session_mode(session_id, mode)
        return JSONResponse({"ok": True, "mode": mode})

    @app.get("/api/session/info")
    async def session_info(request: Request, user: dict = Depends(get_current_user)):
        session_id = request.headers.get("x-session-id", user["id"])
        role       = user.get("role", "standard")
        _, _, mode, _ = _sessions.get(session_id, (None, None, _rbac.default_mode(role), 0))
        return JSONResponse({
            "session_id":     session_id,
            "role":           role,
            "mode":           mode,
            "allowed_modes":  _rbac.allowed_modes(role),
            "allowed_tools":  _rbac.allowed_tools(role),
            "blocked_tools":  _rbac.blocked_tools(role),
        })

    # ── Role management ─────────────────────────────────────────────────
    # ── MCP server management ───────────────────────────────────────────
    @app.get("/api/mcp/servers")
    async def mcp_servers(user: dict = Depends(get_current_user)):
        return JSONResponse(bridge.server_status())

    @app.post("/api/mcp/servers")
    async def mcp_add_server(request: Request, admin: dict = Depends(require_admin)):
        body = await request.json()
        name = body.get("name", "").strip()
        if not name:
            raise HTTPException(400, "name required")
        servers = bridge._suni_servers()
        servers[name] = {
            "command": body.get("command", ""),
            "args":    body.get("args", []),
            "env":     body.get("env", {}),
        }
        bridge.update_config(servers)
        _log.info("[MCP] server added: %s by %s", name, admin["username"])
        return JSONResponse({"ok": True, "name": name})

    @app.put("/api/mcp/servers/{name}")
    async def mcp_update_server(name: str, request: Request, admin: dict = Depends(require_admin)):
        body    = await request.json()
        servers = bridge._suni_servers()
        existing = servers.get(name, {})
        servers[name] = {
            "command": body.get("command", existing.get("command", "")),
            "args":    body.get("args",    existing.get("args", [])),
            "env":     body.get("env",     existing.get("env", {})),
        }
        bridge.update_config(servers)
        _log.info("[MCP] server updated: %s by %s", name, admin["username"])
        return JSONResponse({"ok": True})

    @app.delete("/api/mcp/servers/{name}")
    async def mcp_delete_server(name: str, admin: dict = Depends(require_admin)):
        servers = bridge._suni_servers()
        if name not in servers:
            raise HTTPException(404, "Server not in SUNI config (claude_desktop servers cannot be deleted here)")
        del servers[name]
        bridge.update_config(servers)
        _log.info("[MCP] server removed: %s by %s", name, admin["username"])
        return JSONResponse({"ok": True})

    @app.get("/api/mcp/catalog")
    async def mcp_catalog(user: dict = Depends(get_current_user)):
        from .. import mcp_catalog as _cat
        return JSONResponse(_cat.CATALOG)

    @app.post("/api/mcp/restart")
    async def mcp_restart(admin: dict = Depends(require_admin)):
        _log.info("[MCP] bridge restart requested by %s", admin["username"])
        asyncio.create_task(bridge.restart())
        return JSONResponse({"ok": True, "message": "Bridge restarting"})

    @app.post("/api/mcp/test/{name}")
    async def mcp_test_server(name: str, request: Request, admin: dict = Depends(require_admin)):
        body = await request.json()
        cfg = {"command": body.get("command",""), "args": body.get("args",[]), "env": body.get("env",{})}
        result = await bridge.test_server(name, cfg)
        return JSONResponse(result)

    # ── Skills (procedural memory) ───────────────────────────────────────
    @app.get("/api/skills")
    async def skills_list_api(category: str = "", search: str = "",
                              user: dict = Depends(get_current_user)):
        return JSONResponse(skill_store.list(category=category) if not search
                            else skill_store.search(search))

    @app.get("/api/skills/stats")
    async def skills_stats(user: dict = Depends(get_current_user)):
        return JSONResponse(skill_store.stats())

    @app.get("/api/skills/{slug}")
    async def skill_get(slug: str, user: dict = Depends(get_current_user)):
        s = skill_store.get(slug)
        if not s:
            raise HTTPException(404, "Skill not found")
        return JSONResponse(s)

    @app.post("/api/skills")
    async def skill_create(request: Request, user: dict = Depends(get_current_user)):
        if user.get("role") not in ("admin", "power-user"):
            raise HTTPException(403, "Power-user or admin required to create skills")
        body = await request.json()
        if not body.get("name") or not body.get("body"):
            raise HTTPException(400, "name and body required")
        slug = skill_store.save(
            name=body["name"], body=body["body"],
            category=body.get("category", "general"),
            description=body.get("description", ""),
            tool_count=body.get("tool_count", 0),
        )
        _log.info("[SKILL] created %s by %s", slug, user["username"])
        return JSONResponse({"ok": True, "slug": slug})

    @app.delete("/api/skills/{slug}")
    async def skill_delete_api(slug: str, admin: dict = Depends(require_admin)):
        ok = skill_store.delete(slug)
        if not ok:
            raise HTTPException(404, "Skill not found")
        _log.info("[SKILL] deleted %s by %s", slug, admin["username"])
        return JSONResponse({"ok": True})

    # ── Backup / restore ────────────────────────────────────────────────
    @app.post("/api/backup/create")
    @_limiter.limit("3/minute")
    async def backup_create(request: Request, admin: dict = Depends(require_admin)):
        body = await request.json()
        loop  = asyncio.get_event_loop()
        fname = await loop.run_in_executor(
            None, lambda: _backup.create(
                include_faiss=body.get("include_faiss", False),
                include_logs=body.get("include_logs", False),
                include_uploads=body.get("include_uploads", False),
            )
        )
        _log.info("[BACKUP] created %s by %s", fname, admin["username"])
        # Auto-prune — keep last 20
        await loop.run_in_executor(None, lambda: _backup.prune(20))
        return JSONResponse({"ok": True, "filename": fname})

    @app.get("/api/backup/list")
    async def backup_list(admin: dict = Depends(require_admin)):
        return JSONResponse(_backup.list_backups())

    @app.get("/api/backup/download/{filename}")
    async def backup_download(filename: str, admin: dict = Depends(require_admin)):
        p = _backup.BACKUP_DIR / filename
        if not p.exists() or not p.name.startswith("SUNI_backup_"):
            raise HTTPException(404, "Backup not found")
        return Response(
            content=p.read_bytes(),
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={p.name}"},
        )

    @app.delete("/api/backup/{filename}")
    async def backup_delete(filename: str, admin: dict = Depends(require_admin)):
        ok = _backup.delete(filename)
        if not ok:
            raise HTTPException(404, "Backup not found")
        return JSONResponse({"ok": True})

    @app.post("/api/backup/restore")
    async def backup_restore(request: Request, admin: dict = Depends(require_admin)):
        from fastapi import UploadFile, File
        form  = await request.form()
        file  = form.get("file")
        if not file:
            raise HTTPException(400, "No file uploaded")
        data  = await file.read()
        tmp   = _backup.BACKUP_DIR / f"restore_tmp_{int(time.time())}.zip"
        _backup.BACKUP_DIR.mkdir(exist_ok=True)
        tmp.write_bytes(data)
        try:
            loop   = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, lambda: _backup.restore(tmp))
            _log.info("[BACKUP] restored %d files by %s", len(result["restored"]), admin["username"])
            return JSONResponse(result)
        finally:
            tmp.unlink(missing_ok=True)

    @app.get("/api/roles")
    async def get_roles(user: dict = Depends(get_current_user)):
        return JSONResponse(_rbac.all_roles())

    @app.put("/api/roles")
    async def update_roles(request: Request, admin: dict = Depends(require_admin)):
        body = await request.json()
        _rbac.save(body)
        _log.info("[RBAC] Role config updated by %s", admin["username"])
        return JSONResponse({"ok": True})

    # ── Tool policies (approval-time allow/deny/ask; admin only) ─────────────
    @app.get("/api/policies")
    async def get_policies(admin: dict = Depends(require_admin)):
        from .. import policy as _policy
        return JSONResponse(_policy.raw())

    @app.put("/api/policies")
    async def update_policies(request: Request, admin: dict = Depends(require_admin)):
        from .. import policy as _policy
        body = await request.json()
        try:
            _policy.save(body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        _log.info("[POLICY] Tool policies updated by %s (%d rules)", admin["username"], len(body))
        return JSONResponse({"ok": True})

    # ── User management (admin only) ────────────────────────────────────────
    @app.get("/api/users")
    async def list_users(admin: dict = Depends(require_admin)):
        users = _auth.list_users()
        for u in users:
            settings = _user_settings.get(u["id"])
            u["has_api_key"] = bool(settings.get("anthropic_api_key"))
        return JSONResponse(users)

    @app.post("/api/users")
    async def create_user(request: Request, admin: dict = Depends(require_admin)):
        body = await request.json()
        username = body.get("username", "").strip()
        password = body.get("password", "")
        role     = body.get("role", "standard")
        if not username or not password:
            raise HTTPException(400, "username and password required")
        if len(password) < 8:
            raise HTTPException(400, "Password must be >= 8 characters")
        try:
            user = _auth.create_user(username, password, role)
        except Exception as e:
            raise HTTPException(400, str(e))
        _log.info("[AUTH] User created: %s (role=%s) by %s", username, role, admin["username"])
        return JSONResponse(user)

    @app.put("/api/users/{user_id}")
    async def update_user(user_id: str, request: Request, admin: dict = Depends(require_admin)):
        body = await request.json()
        try:
            user = _auth.update_user(user_id, **{k: body[k] for k in ("role","active","username") if k in body})
        except Exception as e:
            raise HTTPException(400, str(e))
        if not user:
            raise HTTPException(404, "User not found")
        return JSONResponse(user)

    @app.put("/api/users/{user_id}/settings")
    async def admin_update_user_settings(user_id: str, request: Request,
                                         admin: dict = Depends(require_admin)):
        body = await request.json()
        # Only allow safe settings keys — not arbitrary data
        allowed = {"anthropic_api_key", "stt_language", "response_language", "tts_voice", "output_dir"}
        patch   = {k: v for k, v in body.items() if k in allowed}
        if not patch:
            raise HTTPException(400, "No valid settings keys provided")
        # Validate output_dir (same rule as self-service endpoint)
        if "output_dir" in patch:
            out_raw = (patch.get("output_dir") or "").strip()
            if out_raw:
                global_base = suni_config.get("global_output_dir", "").strip()
                if not global_base:
                    raise HTTPException(400, "output_dir requires global_output_dir to be set first")
                try:
                    Path(out_raw).resolve().relative_to(Path(global_base).resolve())
                    patch["output_dir"] = str(Path(out_raw).resolve())
                except ValueError:
                    raise HTTPException(400, f"output_dir must be inside: {global_base}")
            else:
                patch["output_dir"] = ""
        updated = _user_settings.save(user_id, patch)
        # Don't return the raw encrypted key
        safe = {k: v for k, v in updated.items() if k != "anthropic_api_key"}
        safe["has_api_key"] = bool(updated.get("anthropic_api_key"))
        return JSONResponse(safe)

    @app.post("/api/users/{user_id}/reset-password")
    async def reset_password(user_id: str, request: Request, admin: dict = Depends(require_admin)):
        body = await request.json()
        pw = body.get("password", "")
        if len(pw) < 8:
            raise HTTPException(400, "Password must be >= 8 characters")
        _auth.set_password(user_id, pw)
        _log.info("[AUTH] Password reset for %s by %s", user_id, admin["username"])
        return JSONResponse({"ok": True})

    @app.post("/api/users/{user_id}/token")
    async def generate_user_token(user_id: str, admin: dict = Depends(require_admin)):
        raw = _auth.generate_api_token(user_id)
        _log.info("[AUTH] API token generated for %s by %s", user_id, admin["username"])
        return JSONResponse({"token": raw, "note": "Store this — it will not be shown again"})

    @app.delete("/api/users/{user_id}/token")
    async def revoke_user_token(user_id: str, admin: dict = Depends(require_admin)):
        _auth.revoke_api_token(user_id)
        return JSONResponse({"ok": True})

    @app.delete("/api/users/{user_id}")
    async def delete_user(user_id: str, admin: dict = Depends(require_admin)):
        if user_id == admin["id"]:
            raise HTTPException(400, "Cannot delete your own account")
        ok = _auth.delete_user(user_id)
        if not ok:
            raise HTTPException(404, "User not found")
        _log.info("[AUTH] User %s deleted by %s", user_id, admin["username"])
        return JSONResponse({"ok": True})

    # ── Erasure (GDPR Art 17) ───────────────────────────────────────────────
    # Deliberately separate from DELETE /api/users/{id}: that removes an
    # account, this removes a person from every store. Preview first, then an
    # execute that will not fire without the subject's id echoed back.
    @app.get("/api/users/{user_id}/erasure-preview")
    async def erasure_preview(user_id: str, admin: dict = Depends(require_admin)):
        from .. import erasure as _erase
        return JSONResponse(_erase.preview(user_id))

    @app.get("/api/users/{user_id}/export")
    async def export_subject(user_id: str, admin: dict = Depends(require_admin)):
        """Art 15/20 subject access. Read-only, and a download only — nothing
        here mails or ships the file, because the payload is the most sensitive
        one the system can produce and where it goes is the admin's decision."""
        from .. import subject_access as _sa
        payload = _sa.export(user_id)
        _audit.log_event(admin["id"], admin["username"], "user.exported",
                         detail=f"subject access export ({sum(payload['counts'].values())} records)",
                         target_id=user_id)
        name = payload["subject"]["username"] or user_id
        return Response(
            content=_sa.to_json(payload),
            media_type="application/json",
            headers={"Content-Disposition":
                     f'attachment; filename="suni-export-{name}.json"'})

    @app.post("/api/users/{user_id}/erase")
    async def erase_user(user_id: str, request: Request,
                         admin: dict = Depends(require_admin)):
        from .. import erasure as _erase
        if user_id == admin["id"]:
            raise HTTPException(400, "Cannot erase your own account")
        body = {}
        try:
            body = await request.json()
        except Exception:      # noqa: BLE001 — an empty body is a valid request
            pass
        # The subject's id must be echoed back. This is irreversible and spans
        # every store, so a bare POST must not be able to trigger it.
        confirm = str(body.get("confirm_user_id", ""))

        # Hand over the LIVE store if this process holds one, and drop it from
        # the cache either way: MemoryStore._save() rewrites the whole file from
        # memory, so a cached manager would write the erased entries back.
        live = _user_memories.pop(user_id, None)
        store = getattr(live, "store", None) if live else None

        result = _erase.erase(user_id, confirm, memory_store=store)
        if not result["ok"] and "not confirmed" in result["message"]:
            raise HTTPException(400, result["message"])
        # Audited like any other admin action — and this row survives the
        # pseudonymisation above, because it is written after it.
        _audit.log_event(admin["id"], admin["username"], "user.erased",
                         detail=result["message"][:100], target_id=user_id)
        _log.warning("[ERASURE] %s erased by %s — %s",
                     user_id, admin["username"], result["message"])
        return JSONResponse(result)

    # ── Audit log endpoints (admin only) ────────────────────────────────────
    @app.get("/api/audit/logs")
    async def audit_logs(
        request:   Request,
        limit:     int = 50,
        offset:    int = 0,
        user_id:   str = "",
        date_from: str = "",
        date_to:   str = "",
        route:     str = "",
        tool:      str = "",
        user:      dict = Depends(get_current_user),
    ):
        # Non-admins can only see their own audit log
        filter_uid = user_id if user["role"] == "admin" else user["id"]
        rows, total = _audit.query(
            limit=limit, offset=offset,
            user_id=filter_uid or None,
            date_from=date_from or None,
            date_to=date_to or None,
            route=route or None,
            tool=tool or None,
        )
        return JSONResponse({"rows": rows, "total": total})

    @app.get("/api/audit/export")
    async def audit_export(
        user_id:   str = "",
        date_from: str = "",
        date_to:   str = "",
        admin:     dict = Depends(require_admin),
    ):
        csv_data = _audit.export_csv(
            user_id=user_id or None,
            date_from=date_from or None,
            date_to=date_to or None,
        )
        return Response(
            content=csv_data,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=suni_audit.csv"},
        )

    @app.get("/api/audit/stats")
    async def audit_stats(user: dict = Depends(get_current_user)):
        return JSONResponse(_audit.stats())

    @app.get("/api/usage")
    async def usage_summary(days: int = 30, admin: dict = Depends(require_admin)):
        """Per-user token usage over a window (SMB cost visibility)."""
        try:
            days = min(max(1, int(days)), 365)
        except Exception:
            days = 30
        return JSONResponse(_audit.usage_summary(days=days))

    # ── Memory API ──────────────────────────────────────────────────────────
    @app.post("/api/knowledge/compress")
    @_limiter.limit("2/hour")
    async def knowledge_compress(request: Request, admin: dict = Depends(require_admin)):
        """Migrate the FAISS index from FlatIP to IVF-PQ (32× smaller, ~97% recall).
        One-time operation — takes 30–120s. Index is locked during compression."""
        body  = await request.json()
        nlist = int(body.get("nlist", 256))
        loop  = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: doc_store.compress(nlist=nlist)
        )
        if result["ok"]:
            _log.info("[COMPRESS-A4] FAISS compressed %dx by %s",
                      result.get("ratio", 0), admin["username"])
        return JSONResponse(result)

    @app.get("/api/memory/stats")
    async def memory_stats(user: dict = Depends(get_current_user)):
        user_mem  = _get_user_memory(user["id"])
        coll_count = _collective_store.count()
        doc_stats  = doc_store.stats() if doc_store else {}
        meta      = user_mem.store.get_meta()
        life      = user_mem.store.lifecycle_counts()
        return JSONResponse({
            "user": {
                "episodic":          user_mem.store.count(),   # raw total (incl. deprecated)
                "active":            life["active"],
                "deprecated":        life["deprecated"],
                "active_by_type":    life["active_by_type"],
                "store_path":        f"memory/users/{user['id']}/suni_memory.json",
                "last_consolidated": meta.get("last_consolidated"),
            },
            "collective": {
                "episodic": coll_count,
                "doc_store": doc_stats,
            },
        })

    @app.post("/api/memory/promote")
    async def memory_promote(request: Request, user: dict = Depends(get_current_user)):
        """Promote a fact into collective (global) memory — governed + audited."""
        if user.get("role") not in ("admin", "power-user"):
            raise HTTPException(403, "Power-user or admin required")
        body    = await request.json()
        content = body.get("content", "").strip()
        mtype   = body.get("type", "fact")
        visibility = body.get("visibility", "org")
        if visibility not in ("org", "restricted"):
            visibility = "org"
        if not content:
            raise HTTPException(400, "content required")

        from ..memory.manager import embed_nomic as _embed_nomic
        embedding = await _embed_nomic(content)

        # Dedup: skip if a near-identical fact already exists in global memory.
        dup = _collective_store.search(embedding, top_k=1, threshold=0.93)
        if dup:
            _audit.log_event(user["id"], user["username"], "memory.promote.rejected",
                             detail=f"duplicate: {content[:70]}", target_id=dup[0]["id"])
            return JSONResponse(
                {"ok": False, "reason": "duplicate", "existing_id": dup[0]["id"]},
                status_code=409,
            )

        # Sensitivity is detected, not assumed. This field used to be hardcoded
        # to "normal" on every promotion and read by nothing — a governance
        # label that was always true-by-declaration and never true-by-check.
        from .. import sensitivity as _sens
        auto_ok, verdict = _sens.may_auto_approve(content)

        now = datetime.now(timezone.utc).isoformat()
        # Default-deny (governance design §9.2): anything not plainly normal is
        # staged for a human instead of entering shared memory. A candidate is
        # already unreadable to every role — the clearance predicate requires
        # status == "approved" — so staging is a real quarantine, not a label.
        status = "approved" if auto_ok else "candidate"
        meta = {
            "visibility":  visibility,
            "sensitivity": verdict["sensitivity"],
            "status":      status,
            "detection":   {"reasons": verdict["reasons"],
                            "injection": verdict["injection"]},
            "provenance":  {"source_type": "manual", "source_user_id": user["id"],
                            "extracted_at": now},
            "review":      ({"approved_by": user["id"], "approved_at": now,
                             "policy_version": "p1"} if auto_ok else
                            {"approved_by": "", "approved_at": "",
                             "policy_version": "p1"}),
        }
        new_id = _collective_store.add(content, embedding, mtype, metadata=meta)

        # The audit detail deliberately drops the content when something was
        # detected: writing a flagged fact into the audit trail would copy the
        # credential or identifier into the very record kept for compliance.
        if auto_ok:
            _audit.log_event(user["id"], user["username"], "memory.promote.approved",
                             detail=f"[{visibility}] {content[:70]}", target_id=new_id)
            _log.info("[MEMORY] fact promoted to collective by %s (visibility=%s)",
                      user["username"], visibility)
        else:
            _audit.log_event(
                user["id"], user["username"], "memory.promote.candidate",
                detail=f"[{visibility}] staged: {verdict['sensitivity']} "
                       f"({', '.join(verdict['reasons']) or 'unspecified'})",
                target_id=new_id)
            _log.warning("[MEMORY] promotion staged for review by %s — %s (%s)",
                         user["username"], verdict["sensitivity"],
                         ", ".join(verdict["reasons"]))
        return JSONResponse({
            "ok": True, "id": new_id, "visibility": visibility,
            "status": status, "sensitivity": verdict["sensitivity"],
            "reasons": verdict["reasons"],
        })

    # ── Memory governance review queue ──────────────────────────────────────
    # Everything the sensitivity gate declined to auto-approve lands here.
    # Power-user or admin, matching who may promote in the first place.
    def _require_reviewer(user: dict) -> None:
        if user.get("role") not in ("admin", "power-user"):
            raise HTTPException(403, "Power-user or admin required")

    @app.get("/api/memory/candidates")
    async def memory_candidates(include_rejected: bool = False,
                                user: dict = Depends(get_current_user)):
        _require_reviewer(user)
        from ..memory import governance as _gov
        return JSONResponse({
            "candidates": _gov.list_candidates(_collective_store, include_rejected),
            "counts": _gov.counts(_collective_store),
        })

    @app.post("/api/memory/candidates/{memory_id}/approve")
    async def memory_candidate_approve(memory_id: str, request: Request,
                                       user: dict = Depends(get_current_user)):
        _require_reviewer(user)
        from ..memory import governance as _gov
        body = {}
        try:
            body = await request.json()
        except Exception:      # noqa: BLE001 — an empty body is a valid request
            pass
        result = _gov.approve(_collective_store, memory_id, user["id"],
                              user.get("username", ""),
                              note=str(body.get("note", ""))[:200],
                              visibility=body.get("visibility"))
        if not result["ok"]:
            raise HTTPException(404 if result["reason"] == "not found" else 409,
                                result["reason"])
        # Reasons, not content: the entry was staged because of what it
        # contains, so echoing it into the audit trail would defeat the staging.
        _audit.log_event(user["id"], user.get("username", ""),
                         "memory.promote.approved",
                         detail=f"[review] {result['sensitivity']} "
                                f"({', '.join(result['reasons']) or 'no findings'}) "
                                f"-> {result['visibility']}",
                         target_id=memory_id)
        _log.info("[MEMORY] candidate %s approved by %s", memory_id,
                  user.get("username", ""))
        return JSONResponse(result)

    @app.post("/api/memory/candidates/{memory_id}/reject")
    async def memory_candidate_reject(memory_id: str, request: Request,
                                      user: dict = Depends(get_current_user)):
        _require_reviewer(user)
        from ..memory import governance as _gov
        body = {}
        try:
            body = await request.json()
        except Exception:      # noqa: BLE001
            pass
        result = _gov.reject(_collective_store, memory_id, user["id"],
                             user.get("username", ""),
                             note=str(body.get("note", ""))[:200])
        if not result["ok"]:
            raise HTTPException(404 if result["reason"] == "not found" else 409,
                                result["reason"])
        _audit.log_event(user["id"], user.get("username", ""),
                         "memory.promote.rejected",
                         detail=f"[review] {result['sensitivity']} "
                                f"({', '.join(result['reasons']) or 'no findings'})",
                         target_id=memory_id)
        _log.info("[MEMORY] candidate %s rejected by %s", memory_id,
                  user.get("username", ""))
        return JSONResponse(result)

    @app.post("/api/memory/consolidate")
    async def memory_consolidate(request: Request, admin: dict = Depends(require_admin)):
        """Trigger memory consolidation for all users or a specific user (admin only)."""
        body    = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        target  = body.get("user_id") if body else None
        from ..memory import consolidator as _consolidator_mod
        managers: dict = {target: _get_user_memory(target)} if target else dict(_user_memories)
        if not managers:
            return JSONResponse({"consolidated": {}, "note": "no active user memories"})
        results = {}
        for uid, mgr in managers.items():
            since_ts = mgr.store.get_meta().get("last_consolidated")
            results[uid] = await _consolidator_mod.consolidate(
                mgr, model=SUNI_MODEL, since_ts=since_ts
            )
        _log.info("[MEMORY] consolidation triggered by %s: %s", admin["username"], results)
        return JSONResponse({"consolidated": results})

    @app.get("/api/status", dependencies=[Depends(_check_token)])
    async def status():
        stats = memory.stats()
        has_claude_code = _claude_code_present()
        return JSONResponse({
            # "" means nothing is configured and nothing is installed — a
            # real state on a fresh install. Name it rather than rendering an
            # empty field, which looks like a failure to load.
            "model":      SUNI_MODEL or "(none configured)",
            "tts_voice":  TTS_VOICE,
            "memory":     stats["total"],
            "claude_code": has_claude_code,
            "tiers":      {str(t): m.name for t, m in _tier_map.items()},
        })

    @app.get("/api/models/inventory")
    async def models_inventory(request: Request, refresh: int = 0,
                               admin: dict = Depends(require_admin)):
        nonlocal orchestrator, registry, _tier_map
        inv = await _inv.get_inventory(force_refresh=bool(refresh))
        if refresh and inv:
            _tier_map = _inv.tier_map(inv)
            orchestrator, registry = _build_orchestrator(
                memory, skill_store=skill_store, tier_map=_tier_map
            )
        return JSONResponse({
            "models": [
                {"name": m.name, "endpoint": m.endpoint, "tier": m.tier,
                 "params_b": m.params_b, "size_gb": m.size_gb, "online": m.online}
                for m in inv
            ],
            "tier_map": {str(t): m.name for t, m in _tier_map.items()},
        })

    # ── File upload / download ──────────────────────────────────────────────
    _UPLOAD_DIR = Path("memory/uploads")
    _BLOCKED_DL_EXTS = {".pem", ".key", ".pfx", ".p12", ".env", ".reg", ".exe", ".dll", ".bat", ".cmd", ".ps1"}
    _SUNI_FILES_DIR = Path("files")
    _SUNI_FILES_DIR.mkdir(parents=True, exist_ok=True)
    _ALLOWED_DL_ROOTS = [
        Path.home() / "Desktop",
        Path.home() / "Downloads",
        Path.home() / "Documents",
        _SUNI_FILES_DIR,
        _UPLOAD_DIR,
    ]

    # Regex to find /api/files/serve URLs in model responses
    _FILE_SERVE_URL_RE = re.compile(r'(/api/files/serve\?[^\s\)\]>"\']+)', re.IGNORECASE)

    def _inject_dl_token(text: str, jwt: str) -> str:
        """Append &token=<jwt> to file serve URLs so they work without a browser session."""
        def _add(m: re.Match) -> str:
            url = m.group(1)
            return url if "token=" in url else url + "&token=" + jwt
        return _FILE_SERVE_URL_RE.sub(_add, text)

    @app.post("/api/files/upload")
    @_limiter.limit("20/minute")
    async def file_upload(request: Request, user: dict = Depends(get_current_user)):
        import uuid as _uuid
        from ..ingestion.document_loader import load as _doc_load
        form = await request.form()
        file = form.get("file")
        if not file:
            raise HTTPException(400, "No file provided")
        filename = Path(file.filename).name  # strip path components
        # Sanitise: keep only safe chars
        safe_name = "".join(c for c in filename if c.isalnum() or c in "._- ").strip() or "upload"
        dest_dir  = _UPLOAD_DIR / user["id"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        file_id   = _uuid.uuid4().hex[:12]
        dest      = dest_dir / f"{file_id}_{safe_name}"
        data      = await file.read()
        if len(data) > 100 * 1024 * 1024:
            raise HTTPException(413, "File too large (max 100 MB)")
        dest.write_bytes(data)

        # Extract text for context injection
        loop  = asyncio.get_event_loop()
        pages = await loop.run_in_executor(None, lambda: _doc_load(str(dest)))
        text  = "\n\n".join(p["text"] for p in pages if p.get("text"))
        preview = text[:400] if text else "(binary file — no text extracted)"
        _log.info("[UPLOAD] %s uploaded %s (%d bytes, %d chars extracted)",
                  user["username"], safe_name, len(data), len(text))
        return JSONResponse({
            "file_id":   file_id,
            "filename":  safe_name,
            "path":      str(dest),
            "size":      len(data),
            "word_count": len(text.split()),
            "preview":   preview,
            "has_text":  bool(text),
        })

    @app.get("/api/files/serve")
    async def file_serve(
        path: str,
        token: str | None = None,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="x-api-key"),
    ):
        # Accept JWT in query param (for clickable file links) or standard header auth
        try:
            user = get_current_user(authorization, x_api_key)
        except HTTPException:
            user = None
            if token:
                user = _auth.verify_token(token) or _auth.verify_api_token(token)
            if not user:
                raise HTTPException(status_code=401, detail="Not authenticated")
        p = Path(path).resolve()
        if p.suffix.lower() in _BLOCKED_DL_EXTS:
            raise HTTPException(403, "File type not allowed for download")
        # Build the per-request allowed roots: static base + global output dir + user output dir
        # Read config at request time so admin changes take effect without a restart.
        allowed = list(_ALLOWED_DL_ROOTS) + [_UPLOAD_DIR / user["id"]]
        global_out = suni_config.get("global_output_dir", "").strip()
        if global_out:
            allowed.append(Path(global_out).resolve())
        # Per-user output_dir: re-validate containment at serve time (don't trust stored value alone)
        user_out_raw = _user_settings.get(user["id"]).get("output_dir", "").strip()
        if user_out_raw and global_out:
            try:
                user_out_p = Path(user_out_raw).resolve()
                user_out_p.relative_to(Path(global_out).resolve())
                allowed.append(user_out_p)
            except ValueError:
                pass  # stored value no longer valid — silently ignore
        if not any(p.is_relative_to(r.resolve()) for r in allowed):
            raise HTTPException(403, "Path not in an allowed download directory")
        # Extra guard: block sensitive filenames regardless of location
        if p.name.lower() in {"users.db", "jwt_secret.txt", "api_key.txt", ".env"}:
            raise HTTPException(403, "File not allowed")
        if not p.exists() or not p.is_file():
            raise HTTPException(404, "File not found")
        import mimetypes as _mt
        mime = _mt.guess_type(str(p))[0] or "application/octet-stream"
        return Response(
            content=p.read_bytes(),
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{p.name}"'},
        )

    @app.post("/api/chat")
    @_limiter.limit("20/minute")
    async def chat(request: Request, user: dict = Depends(get_current_user)):
        body            = await request.json()
        message         = str(body.get("message", ""))[:_MAX_CHAT_CHARS]
        session_id      = body.get("session_id") or request.headers.get("x-session-id") or user["id"]
        conversation_id = body.get("conversation_id")
        user_role       = user.get("role", "standard")
        def_mode        = _rbac.default_mode(user_role)

        # Chat UI: use conversation_id as session key; rebuild context from DB if not live
        if conversation_id:
            session_id = conversation_id
            if conversation_id not in _sessions:
                conv = _conversations.get_conversation(conversation_id, user["id"])
                if not conv:
                    raise HTTPException(404, "Conversation not found")
                from ..core.message import Message as _Msg, Role as _MsgRole
                stored = _conversations.get_messages(conversation_id)
                ctx = Context()
                for m in stored:
                    r = _MsgRole.USER if m["role"] == "user" else _MsgRole.ASSISTANT
                    ctx.add(_Msg(role=r, content=m["content"]))
                _sessions[conversation_id] = (ctx, asyncio.Lock(), def_mode, time.time())

        context, _ctx_lock, conv_mode = _get_context(session_id, default_mode=def_mode)
        if conversation_id:
            context.set("conversation_id", conversation_id)
        if "mode" in body and _rbac.can_use_mode(user_role, body["mode"]):
            _set_session_mode(session_id, body["mode"])
            conv_mode = body["mode"]

        # Agent profile, if one was selected. Loaded server-side by slug and
        # checked against what THIS user may use — the client sends a name, never
        # a profile, so a caller cannot hand in a forged definition. Grants are
        # still re-resolved inside the orchestrator against the caller's role, so
        # this check is about visibility, not privilege.
        _agent_profile = None
        _agent_slug = str(body.get("agent") or "").strip()
        if _agent_slug:
            from .. import agents as _agents
            _visible = {a["slug"] for a in _agents.list_for_user(user["id"], user_role)}
            if _agent_slug in _visible:
                _agent_profile = _agents.get(_agent_slug)
                if _agent_profile and not _agent_profile.get("enabled", True):
                    _agent_profile = None
                if _agent_profile:
                    _agent_profile["_username"] = user["username"]
            else:
                _log.warning("[AGENT] %s requested agent %r they cannot use",
                             user["username"], _agent_slug)

        # Per-user language and MCP preferences
        _prefs     = _user_settings.get(user["id"])
        _resp_lang = _prefs.get("response_language", "")
        _user_mcp  = _prefs.get("allowed_mcp_servers")   # None = use role default

        # File attachments — inject extracted text as context before the model runs.
        # Image attachments are split off for the vision path (when a VLM is
        # configured); otherwise they fall through to the "binary file" note below.
        from .. import vision as _vision
        _file_ids     = body.get("file_ids") or []
        _file_ctxs    = []
        _image_paths  = []
        _has_image    = False
        for fid in _file_ids[:5]:   # max 5 files per message
            try:
                from ..ingestion.document_loader import load as _dl
                # Locate the upload: search user's upload dir for matching file_id prefix
                upload_dir = _UPLOAD_DIR / user["id"]
                matches    = list(upload_dir.glob(f"{fid}_*")) if upload_dir.exists() else []
                if matches:
                    p     = matches[0]
                    abs_p = str(p.resolve())   # absolute — CC runs from a different cwd
                    if _vision.is_image(abs_p):
                        _has_image = True
                    if _vision.is_image(abs_p) and _vision.enabled():
                        _image_paths.append(abs_p)   # → vision handler, not text context
                        continue
                    pages = await asyncio.get_event_loop().run_in_executor(None, lambda p=p: _dl(str(p)))
                    text  = "\n\n".join(pg["text"] for pg in pages if pg.get("text"))
                    if text:
                        _file_ctxs.append(
                            f"[Attached file: {p.name}]\n"
                            f"Path (readable on this machine): {abs_p}\n"
                            f"{text[:8000]}"
                        )
                    else:
                        _file_ctxs.append(
                            f"[Attached file: {p.name}]\n"
                            f"Path (readable on this machine): {abs_p}\n"
                            f"(No text auto-extracted — read or parse the file directly if needed.)"
                        )
            except Exception:
                pass

        # Face stage: let SUNI point at image regions via a ```suni-focus``` block.
        # Only for the face UI (other surfaces don't render it). Uses the model's
        # own vision (Claude Code reads the image) — no separate VLM required.
        if _has_image and str(body.get("ui", "")).lower() == "face":
            _file_ctxs.append(
                "[Stage — image annotation]\n"
                "The user is viewing the attached image(s) on a visual stage. To point at a "
                "region of an image, you MAY include a fenced block exactly like:\n"
                "```suni-focus\n"
                '{"target": "<image filename>", "box": [x, y, w, h], "note": "<short label>"}\n'
                "```\n"
                "x,y = top-left corner; w,h = width/height; all as fractions of the image "
                "(0.0-1.0, origin top-left). Estimate the region from what you see. Emit at "
                "most a few, only when it genuinely helps the user locate something. Keep the "
                "rest of your reply as normal prose."
            )

        user_memory = _get_user_memory(user["id"])

        async def generate():
            t0 = time.time()
            # Per-request token accumulator — chat() calls sum into this; totals
            # go onto the audit row below (mutable object so nested calls update it).
            from .. import usage as _usage
            _usage_acc, _usage_tok = _usage.start()
            # Event queue: orchestrator pushes tool/skill events here while running
            _evt_queue: asyncio.Queue = asyncio.Queue()

            def _event_cb(evt: dict) -> None:
                _evt_queue.put_nowait(evt)

            async def _drain_events():
                """Yield any queued events without blocking."""
                while not _evt_queue.empty():
                    try:
                        evt = _evt_queue.get_nowait()
                        yield f"data: {json.dumps(evt)}\n\n"
                    except asyncio.QueueEmpty:
                        break

            try:
                async with _ctx_lock:
                    # Pre-inject file content as system context
                    if _file_ctxs:
                        from ..core.message import Message as _FMsg, Role as _FRole
                        for fc in _file_ctxs:
                            context.add(_FMsg(role=_FRole.SYSTEM, content=fc, agent="upload"))
                    _claude_api_key = _user_settings.get_decrypted_api_key(user["id"])
                    # Run the orchestrator as a task and stream progress events LIVE
                    # while it works. Essential for the multi-minute collaborate mode
                    # (draft/critique/synthesize phases) — otherwise every event would
                    # buffer until the run finished. Benefits tool events too.
                    _run_task = asyncio.create_task(orchestrator._safe_run(
                        message, context,
                        agent_profile=_agent_profile,
                        memory_override=user_memory,
                        user_role=user_role,
                        conv_mode=conv_mode,
                        response_language=_resp_lang,
                        user_mcp_servers=_user_mcp,
                        event_cb=_event_cb,
                        user_id=user["id"],
                        claude_api_key=_claude_api_key,
                        images=_image_paths,
                    ))
                    while True:
                        try:
                            evt = await asyncio.wait_for(_evt_queue.get(), timeout=0.25)
                            yield f"data: {json.dumps(evt)}\n\n"
                        except asyncio.TimeoutError:
                            if _run_task.done():
                                break
                    response = await _run_task   # re-raises any run error to the handler
                # Flush any events queued between the last drain and completion
                async for evt_line in _drain_events():
                    yield evt_line

                # Inject download tokens into any file serve URLs so they work on click
                if "/api/files/serve" in response:
                    _user_jwt = _auth.create_access_token(user)
                    response = _inject_dl_token(response, _user_jwt)

                words = response.split(" ")
                for i, word in enumerate(words):
                    chunk = word + (" " if i < len(words) - 1 else "")
                    payload = json.dumps({"type": "token", "text": chunk})
                    yield f"data: {payload}\n\n"
                    await asyncio.sleep(0.018)
                yield f"data: {json.dumps({'type': 'done', 'text': response})}\n\n"

                # Persist messages to conversations DB
                if conversation_id:
                    _conversations.add_message(conversation_id, "user", message)
                    _conversations.add_message(conversation_id, "assistant", response)
                    all_msgs = _conversations.get_messages(conversation_id)
                    if len(all_msgs) == 2:  # first exchange — auto-title from user message
                        title = (message[:52].rstrip() + "…") if len(message) > 52 else message
                        _conversations.update_title(conversation_id, user["id"], title)

                _audit.log(
                    user_id=user["id"], username=user["username"],
                    session_id=session_id,
                    ip_address=request.client.host if request.client else "",
                    query_preview=message[:100],
                    route="chat",
                    duration_s=round(time.time() - t0, 2),
                    prompt_tokens=_usage_acc.prompt,
                    gen_tokens=_usage_acc.gen,
                    agent_slug=(_agent_profile or {}).get("slug", ""),
                )
            except Exception as e:
                _log.error("[CHAT_ERR] %s", e, exc_info=True)
                yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
            finally:
                _usage.reset(_usage_tok)
                # Cancel any approvals this user left pending (covers disconnect)
                from .. import approval as _ap
                cancelled = _ap.cancel_all_for_user(user["id"])
                if cancelled:
                    _log.info("[APPROVAL] cancelled %d pending approval(s) on stream end for %s",
                              cancelled, user["username"])

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/whatsapp")
    async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
        """Twilio WhatsApp webhook — receives inbound messages, replies async."""
        # H6: validate Twilio signature to prevent spoofed webhook calls.
        # Fail closed: without an auth token there is no way to authenticate the
        # caller, so refuse rather than process an unauthenticated message.
        twilio_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        if not twilio_token:
            _log.warning("[WHATSAPP] TWILIO_AUTH_TOKEN not configured — webhook rejected (fail closed)")
            return Response(status_code=403)
        try:
            from twilio.request_validator import RequestValidator
            validator = RequestValidator(twilio_token)
            form_body = await request.form()
            signature = request.headers.get("X-Twilio-Signature", "")
            if not validator.validate(str(request.url), dict(form_body), signature):
                _log.warning("[WHATSAPP] invalid Twilio signature — request rejected")
                return Response(status_code=403)
            form = form_body
        except Exception as e:
            _log.error("[WHATSAPP] signature validation error: %s", e)
            return Response(status_code=403)

        from_   = form.get("From", "")
        body    = (form.get("Body") or "").strip()
        if not body or not from_:
            return Response(status_code=204)

        # Per-user conversation context
        if from_ not in wa_contexts:
            wa_contexts[from_] = Context()
        user_ctx = wa_contexts[from_]

        # Per-sender memory isolation (from_ is like "whatsapp:+3519..."). Sanitize
        # to a filesystem-safe pseudo user-id so each sender keeps its own memory.
        _wa_uid = "whatsapp_" + re.sub(r'[^0-9A-Za-z]+', '_', from_)

        async def _process():
            try:
                # Least-privilege: inbound channels are not per-sender authenticated,
                # so they must not carry admin/code-execution rights. "standard"
                # blocks run_shell / claude_task / T5-direct (see rbac.DEFAULTS).
                response = await orchestrator._safe_run(
                    body, user_ctx, memory_override=_get_user_memory(_wa_uid),
                    user_role="standard", user_id=_wa_uid)
                await wa_send(from_, response)
            except Exception as e:
                await wa_send(from_, f"I appear to have encountered a difficulty: {e}")

        background_tasks.add_task(_process)
        # Return empty 200 immediately — Twilio requires fast acknowledgement
        return Response(status_code=200)

    @app.get("/whatsapp/status")
    async def whatsapp_status():
        return JSONResponse({
            "configured": wa_configured(),
            "webhook_url": f"{os.environ.get('SUNI_PUBLIC_URL', 'http://localhost:8765')}/whatsapp",
        })

    @app.post("/telegram")
    async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
        """Telegram bot webhook — receives updates, replies async."""
        # Fail closed: without a configured secret token, inbound updates cannot
        # be authenticated, so refuse rather than process a spoofable message.
        if not _TG_SECRET:
            _log.warning("[TELEGRAM] TELEGRAM_SECRET_TOKEN not configured — webhook rejected (fail closed)")
            return Response(status_code=403)
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != _TG_SECRET:
            _log.warning("[TELEGRAM] invalid secret token — request rejected")
            return Response(status_code=403)

        try:
            data = await request.json()
        except Exception:
            return Response(status_code=400)

        message = data.get("message") or data.get("edited_message")
        if not message:
            return Response(status_code=200)

        chat_id = message.get("chat", {}).get("id")
        text    = (message.get("text") or "").strip()
        if not chat_id or not text:
            return Response(status_code=200)

        # Shared allow-list gate + orchestrator dispatch (same path as long-poll).
        background_tasks.add_task(_handle_tg, chat_id, text)
        return Response(status_code=200)

    @app.get("/telegram/status")
    async def telegram_status():
        return JSONResponse({
            "configured":  tg_configured(),
            "webhook_url": f"{os.environ.get('SUNI_PUBLIC_URL', 'http://localhost:8765')}/telegram",
        })

    @app.post("/api/tts")
    @_limiter.limit("40/minute")
    async def tts(request: Request, user: dict = Depends(get_current_user)):
        import re
        body  = await request.json()
        text  = body.get("text", "")
        clean = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
        clean = re.sub(r'#{1,6}\s+', '', clean)
        clean = re.sub(r'`([^`]+)`', r'\1', clean)
        clean = re.sub(r'\n+', ' ', clean).strip()

        _settings    = _user_settings.get(user["id"]) or {}
        _saved_voice = str(_settings.get("tts_voice") or "").strip()

        # Self-hosted TTS (opt-in) — audio stays in your infra. Do NOT fall back
        # to edge-tts on failure: a privacy-motivated deployment must not silently
        # leak text to the cloud. Return 503 (no audio) instead. Server voice IDs
        # are backend-specific, so we don't validate/remap them against edge names.
        from .. import tts as _tts
        if _tts.enabled():
            voice = body.get("voice") or _saved_voice or TTS_VOICE
            try:
                audio = await _tts.synthesize(clean, voice)
                return StreamingResponse(io.BytesIO(audio), media_type="audio/mpeg")
            except _tts.TTSError as e:
                _log.warning("[TTS] self-hosted TTS failed (no cloud fallback): %s", e)
                return Response(status_code=503)

        # edge-tts: honour the user's saved voice only if it's a real edge voice;
        # otherwise speak in the reply language so Portuguese text isn't read with
        # an English accent — and a stale/invalid voice ID can't cause a silent
        # 503. We deliberately ignore any client-sent voice for this decision: the
        # browser echoes back a possibly-English global default, which must not
        # override a Portuguese speaker's language.
        if _saved_voice in _EDGE_TTS_VOICE_SET:
            voice = _saved_voice
        else:
            voice = (_voice_for_lang(_settings.get("response_language")
                                     or _settings.get("stt_language"))
                     or TTS_VOICE)
        try:
            buf = io.BytesIO()
            communicate = edge_tts.Communicate(clean, voice)
            async with asyncio.timeout(15):
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        buf.write(chunk["data"])
            buf.seek(0)
            return StreamingResponse(buf, media_type="audio/mpeg")
        except Exception:
            return Response(status_code=503)

    @app.post("/api/stt/transcribe")
    @_limiter.limit("30/minute")
    async def stt_transcribe(request: Request, user: dict = Depends(get_current_user)):
        """Server-side transcription (opt-in). Accepts a multipart audio file,
        forwards to the configured whisper endpoint, returns {text}."""
        from .. import stt as _stt
        if not _stt.enabled():
            raise HTTPException(400, "Server-side speech-to-text is not enabled.")
        form  = await request.form()
        audio = form.get("audio")
        if audio is None or not hasattr(audio, "read"):
            raise HTTPException(400, "No audio file provided.")
        data = await audio.read()
        if not data:
            raise HTTPException(400, "Empty audio.")
        if len(data) > _stt._MAX_AUDIO_BYTES:
            raise HTTPException(413, "Audio too large.")
        try:
            text = await _stt.transcribe(
                data,
                filename=getattr(audio, "filename", None) or "audio.webm",
                content_type=getattr(audio, "content_type", None) or "audio/webm",
            )
        except _stt.STTError as e:
            raise HTTPException(502, str(e))
        return JSONResponse({"text": text})

    # -----------------------------------------------------------------------
    # Admin UI
    # -----------------------------------------------------------------------

    @app.get("/admin")
    async def admin(request: Request):
        user, redir = _check_page_auth(request, admin_required=True)
        if redir: return redir
        return HTMLResponse(_inject_token(ADMIN_FILE.read_text(encoding="utf-8")))

    @app.get("/i18n.js")
    async def i18n_js():
        return Response(content=I18N_FILE.read_text(encoding="utf-8"),
                        media_type="application/javascript; charset=utf-8")

    @app.get("/approval_ui.js")
    async def approval_ui_js():
        """Shared approval prompt for the two voice-first surfaces. chat.html
        has its own richer card; these had none, which is why every gated tool
        timed out there."""
        return Response(
            content=(Path(__file__).parent / "approval_ui.js").read_text(encoding="utf-8"),
            media_type="application/javascript; charset=utf-8")

    @app.get("/persona-image")
    async def persona_image():
        """Serve the user-supplied portrait for the Persona page's photoreal mode.
        Drop a file named persona.(png|jpg|jpeg|webp) into suni/web/. 404 if absent
        (the Persona page then falls back to its procedural face)."""
        web_dir = Path(__file__).parent
        for ext in ("png", "jpg", "jpeg", "webp"):
            p = web_dir / f"persona.{ext}"
            if p.exists():
                mt = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
                return Response(content=p.read_bytes(), media_type=mt,
                                headers={"Cache-Control": "no-cache"})
        raise HTTPException(404, "No persona image configured")

    @app.get("/persona-model")
    async def persona_model():
        """Serve a user-supplied head mesh (OBJ) for the Persona page's wireframe
        mode. Prefers persona_b.obj, falls back to persona.obj. 404 if absent."""
        web_dir = Path(__file__).parent
        for name in ("persona_b.obj", "persona.obj"):
            p = web_dir / name
            if p.exists():
                return Response(content=p.read_text(encoding="utf-8", errors="ignore"),
                                media_type="text/plain; charset=utf-8",
                                headers={"Cache-Control": "no-cache"})
        raise HTTPException(404, "No persona model configured")

    @app.get("/persona-model-solid")
    async def persona_model_solid():
        """Higher-poly mesh for the Persona page's solid (shaded) mode. Prefers
        persona_b.obj, then persona_solid.obj, then persona.obj."""
        web_dir = Path(__file__).parent
        for name in ("persona_b.obj", "persona_solid.obj", "persona.obj"):
            p = web_dir / name
            if p.exists():
                return Response(content=p.read_text(encoding="utf-8", errors="ignore"),
                                media_type="text/plain; charset=utf-8",
                                headers={"Cache-Control": "no-cache"})
        raise HTTPException(404, "No persona solid model configured")

    @app.get("/architecture")
    async def architecture(request: Request):
        user, redir = _check_page_auth(request)
        if redir: return redir
        return HTMLResponse(_inject_token(ARCH_FILE.read_text(encoding="utf-8")))

    @app.get("/chat")
    async def chat_page(request: Request):
        user, redir = _check_page_auth(request)
        if redir: return redir
        return HTMLResponse(_inject_jwt(CHAT_FILE.read_text(encoding="utf-8"), user))

    # ── Conversations (Chat UI) ─────────────────────────────────────────────

    @app.get("/api/conversations")
    async def list_conversations_api(user: dict = Depends(get_current_user)):
        convs = _conversations.list_conversations(user["id"])
        return JSONResponse({"conversations": convs})

    @app.post("/api/conversations")
    async def create_conversation_api(request: Request, user: dict = Depends(get_current_user)):
        body = await request.json()
        title = str(body.get("title", "Nova conversa"))[:100]
        conv = _conversations.create_conversation(user["id"], title)
        return JSONResponse(conv)

    @app.get("/api/conversations/{conv_id}")
    async def get_conversation_api(conv_id: str, user: dict = Depends(get_current_user)):
        conv = _conversations.get_conversation(conv_id, user["id"])
        if not conv:
            raise HTTPException(404, "Conversation not found")
        msgs = _conversations.get_messages(conv_id)
        return JSONResponse({"conversation": conv, "messages": msgs})

    @app.put("/api/conversations/{conv_id}")
    async def update_conversation_api(
        conv_id: str, request: Request, user: dict = Depends(get_current_user)
    ):
        body = await request.json()
        title = str(body.get("title", "")).strip()[:100]
        if not title:
            raise HTTPException(400, "title required")
        ok = _conversations.update_title(conv_id, user["id"], title)
        if not ok:
            raise HTTPException(404, "Conversation not found")
        return JSONResponse({"ok": True})

    @app.delete("/api/conversations/{conv_id}")
    async def delete_conversation_api(conv_id: str, user: dict = Depends(get_current_user)):
        _sessions.pop(conv_id, None)  # evict in-memory context
        ok = _conversations.delete_conversation(conv_id, user["id"])
        if not ok:
            raise HTTPException(404, "Conversation not found")
        return JSONResponse({"ok": True})

    # ── Calendar ──────────────────────────────────────────────────────────

    @app.get("/api/calendar/status")
    async def calendar_status(user: dict = Depends(get_current_user)):
        from .. import calendar_client as _cal
        configured = _cal.is_configured()
        cfg = _cal._get_config()
        return JSONResponse({
            "configured": configured,
            "url":        cfg.get("url", ""),
            "username":   cfg.get("username", ""),
            "calendar":   cfg.get("calendar", ""),
        })

    @app.get("/api/calendar/events")
    async def calendar_events(days: int = 7, user: dict = Depends(get_current_user)):
        from .. import calendar_client as _cal
        if not _cal.is_configured():
            return JSONResponse({"events": [], "configured": False})
        events = _cal.get_events(days_ahead=days)
        return JSONResponse({"events": events, "configured": True})

    @app.get("/api/calendar/calendars")
    async def calendar_list(user: dict = Depends(get_current_user)):
        from .. import calendar_client as _cal
        if not _cal.is_configured():
            return JSONResponse({"calendars": [], "configured": False})
        try:
            names = _cal.list_calendars()
            return JSONResponse({"calendars": names, "configured": True})
        except Exception as e:
            raise HTTPException(502, f"CalDAV error: {e}")

    # ── Approval gate ─────────────────────────────────────────────────────

    @app.post("/api/approval/{approval_id}")
    async def approval_resolve(
        approval_id: str,
        request: Request,
        user: dict = Depends(get_current_user),
    ):
        from .. import approval as _ap
        body     = await request.json()
        decision = str(body.get("decision", "deny")).lower()
        if decision not in ("allow", "deny"):
            raise HTTPException(400, "decision must be 'allow' or 'deny'")
        always = bool(body.get("always_allow", False))

        ok = _ap.resolve_approval(approval_id, decision, user_id=user["id"])
        if not ok:
            raise HTTPException(404, "Approval request not found or already resolved")

        tool = body.get("tool", "?")
        if always and decision == "allow":
            if tool and tool != "?":
                _ap.add_trust_rule(user["id"], tool)
                _log.info("[APPROVAL] always-allow added: user=%s tool=%s", user["username"], tool)

        summary = body.get("summary", tool)
        _audit.log(
            user_id=user["id"], username=user["username"],
            session_id=approval_id,
            ip_address=request.client.host if request.client else "",
            query_preview=f"approval:{decision}:{summary[:60]}",
            route="approval",
            tools_called=[tool] if tool != "?" else [],
            duration_s=0,
            approved_by="always-allow" if (always and decision == "allow") else decision,
        )
        return JSONResponse({"ok": True, "decision": decision})

    @app.get("/api/approval/history")
    async def approval_history(
        limit:   int = 50,
        offset:  int = 0,
        tool:    str = "",
        user:    dict = Depends(get_current_user),
    ):
        """Approval audit trail. Non-admins see only their own records."""
        filter_uid = None if user.get("role") == "admin" else user["id"]
        rows, total = _audit.query(
            limit=limit, offset=offset,
            user_id=filter_uid,
            route="approval",
            tool=tool or None,
        )
        return JSONResponse({"rows": rows, "total": total})

    @app.get("/api/approval/pending")
    async def approval_pending(user: dict = Depends(get_current_user)):
        from .. import approval as _ap
        return JSONResponse({"pending": _ap.pending_for_user(user["id"])})

    @app.get("/api/approval/trust")
    async def approval_trust_list(user: dict = Depends(get_current_user)):
        from .. import approval as _ap
        return JSONResponse({"rules": _ap.list_trust_rules(user["id"])})

    @app.delete("/api/approval/trust/{tool_name}")
    async def approval_trust_remove(tool_name: str, user: dict = Depends(get_current_user)):
        from .. import approval as _ap
        _ap.remove_trust_rule(user["id"], tool_name)
        return JSONResponse({"ok": True})

    # ── Contacts (CRM-lite) ────────────────────────────────────────────────

    @app.get("/api/contacts")
    async def contacts_list(
        q: str = "", limit: int = 50, offset: int = 0,
        user: dict = Depends(get_current_user),
    ):
        from .. import contacts as _c
        if q:
            rows = _c.search_contacts(q, limit=limit)
            return JSONResponse({"contacts": rows, "total": len(rows)})
        rows, total = _c.list_contacts(limit=limit, offset=offset)
        return JSONResponse({"contacts": rows, "total": total})

    @app.post("/api/contacts")
    async def contacts_create(request: Request, user: dict = Depends(get_current_user)):
        from .. import contacts as _c
        body = await request.json()
        name = str(body.get("name", "")).strip()
        if not name:
            raise HTTPException(400, "name required")
        contact = _c.add_contact(
            name=name,
            company=str(body.get("company", "")),
            email=str(body.get("email", "")),
            phone=str(body.get("phone", "")),
            notes=str(body.get("notes", "")),
            tags=body.get("tags") or [],
        )
        _log.info("[CONTACTS] created %s by %s", contact["id"], user["username"])
        return JSONResponse(contact, status_code=201)

    @app.get("/api/contacts/{contact_id}")
    async def contacts_get(contact_id: str, user: dict = Depends(get_current_user)):
        from .. import contacts as _c
        contact = _c.get_contact(contact_id)
        if not contact:
            raise HTTPException(404, "Contact not found")
        return JSONResponse(contact)

    @app.put("/api/contacts/{contact_id}")
    async def contacts_update(
        contact_id: str, request: Request, user: dict = Depends(get_current_user)
    ):
        from .. import contacts as _c
        body    = await request.json()
        updated = _c.update_contact(
            contact_id,
            name=body.get("name"),
            company=body.get("company"),
            email=body.get("email"),
            phone=body.get("phone"),
            notes=body.get("notes"),
            tags=body.get("tags"),
        )
        if not updated:
            raise HTTPException(404, "Contact not found")
        return JSONResponse(updated)

    @app.post("/api/contacts/{contact_id}/notes")
    async def contacts_append_note(
        contact_id: str, request: Request, user: dict = Depends(get_current_user)
    ):
        from .. import contacts as _c
        body = await request.json()
        note = str(body.get("note", "")).strip()
        if not note:
            raise HTTPException(400, "note required")
        updated = _c.append_note(contact_id, note)
        if not updated:
            raise HTTPException(404, "Contact not found")
        return JSONResponse(updated)

    @app.delete("/api/contacts/{contact_id}")
    async def contacts_delete(contact_id: str, admin: dict = Depends(require_admin)):
        from .. import contacts as _c
        ok = _c.delete_contact(contact_id)
        if not ok:
            raise HTTPException(404, "Contact not found")
        _log.info("[CONTACTS] deleted %s by %s", contact_id, admin["username"])
        return JSONResponse({"ok": True})

    # ── Monitor (web/news watch) ───────────────────────────────────────────

    @app.get("/api/monitor")
    async def monitor_list_api(user: dict = Depends(get_current_user)):
        from .. import monitor as _m
        return JSONResponse({"watches": _m.list_watches(user_id=user["id"])})

    @app.post("/api/monitor")
    async def monitor_add_api(request: Request, user: dict = Depends(get_current_user)):
        from .. import monitor as _m
        body  = await request.json()
        mtype = str(body.get("type", "topic")).lower()
        value = str(body.get("value", "")).strip()
        if mtype not in ("topic", "feed"):
            raise HTTPException(400, "type must be 'topic' or 'feed'")
        if not value:
            raise HTTPException(400, "value required")
        w = _m.add_watch(mtype, value, user_id=user["id"])
        return JSONResponse(w, status_code=201)

    @app.delete("/api/monitor/{watch_id}")
    async def monitor_remove_api(watch_id: str, user: dict = Depends(get_current_user)):
        from .. import monitor as _m
        ok = _m.remove_watch(watch_id)
        if not ok:
            raise HTTPException(404, "Watch item not found")
        return JSONResponse({"ok": True})

    @app.get("/api/monitor/alerts")
    async def monitor_alerts_api(limit: int = 50, user: dict = Depends(get_current_user)):
        from .. import monitor as _m
        return JSONResponse({"alerts": _m.get_recent_alerts(limit=limit)})

    @app.post("/api/monitor/check")
    async def monitor_check_now(
        background_tasks: BackgroundTasks,
        admin: dict = Depends(require_admin),
    ):
        from .. import monitor as _m
        async def _run():
            alerts = await _m.run_check()
            _log.info("[MONITOR] manual check: %d new alert(s)", len(alerts))
        background_tasks.add_task(_run)
        return JSONResponse({"ok": True, "message": "Check started in background"})

    # ── Updates ────────────────────────────────────────────────────────────
    # Admin only, without exception. This replaces the code the server is
    # running; a non-admin able to trigger it would be the widest privilege
    # escalation in the codebase, wider than any tool.

    @app.post("/api/logship/test", dependencies=[Depends(_check_token)])
    async def logship_test_api(request: Request, user: dict = Depends(require_admin)):
        """Send one line to the configured target and report what happened.

        Admin-only: this reaches out to a network endpoint using stored
        credentials. A blank secret in the body means "use the stored one", the
        same rule the config form follows, so testing does not require retyping
        a password that is deliberately never echoed back.
        """
        from .. import log_ship as _ship
        body = await request.json()
        stored = suni_config.load()
        block = {
            "enabled": True,
            "type":        body.get("type")        or stored.get("logship_type", ""),
            "host":        body.get("host")        or stored.get("logship_host", ""),
            "port":        body.get("port")        or stored.get("logship_port", 0),
            "protocol":    body.get("protocol")    or stored.get("logship_protocol", "udp"),
            "app_name":    body.get("app_name")    or stored.get("logship_app_name", "suni"),
            "url":         body.get("url")         or stored.get("logship_url", ""),
            "auth_header": body.get("auth_header") or stored.get("logship_auth_header", "Authorization"),
            "auth_prefix": body.get("auth_prefix") or stored.get("logship_auth_prefix", "Bearer"),
            "username":    body.get("username")    or stored.get("logship_username", ""),
            "remote_dir":  body.get("remote_dir")  or stored.get("logship_remote_dir", "/"),
            "token":       (body.get("token")    or "").strip() or stored.get("logship_token", ""),
            "password":    (body.get("password") or "").strip() or stored.get("logship_password", ""),
        }
        result = _ship.test(block)
        _audit.log_event(user["id"], user["username"],
                         "logship.test.ok" if result.get("ok") else "logship.test.failed",
                         detail=_ship.describe(block)[:100])
        return JSONResponse(result)

    @app.get("/api/logship/status")
    async def logship_status_api(user: dict = Depends(require_admin)):
        from .. import log_ship as _ship
        cfg = suni_config.load()
        return JSONResponse({
            "enabled": bool(cfg.get("logship_enabled", False)),
            "type":    cfg.get("logship_type", ""),
            "level":   cfg.get("logship_level", "INFO"),
            # Never the credential — only whether one is stored.
            "target":  _ship.describe({
                "type":       cfg.get("logship_type", ""),
                "host":       cfg.get("logship_host", ""),
                "port":       cfg.get("logship_port", 0),
                "protocol":   cfg.get("logship_protocol", "udp"),
                "url":        cfg.get("logship_url", ""),
                "username":   cfg.get("logship_username", ""),
                "remote_dir": cfg.get("logship_remote_dir", "/"),
            }),
        })

    @app.get("/api/version")
    async def version_api(user: dict = Depends(get_current_user)):
        """What is running. Available to any signed-in user, unlike the update
        routes — knowing the version is not the same as being able to change it."""
        from .. import updater as _up
        return JSONResponse(_up.version())

    @app.get("/api/update/status")
    async def update_status_api(user: dict = Depends(require_admin)):
        from .. import updater as _up
        return JSONResponse({"status": _up.status()})

    @app.post("/api/update/apply")
    async def update_apply_api(request: Request, user: dict = Depends(require_admin)):
        from .. import updater as _up
        body = {}
        try:
            body = await request.json()
        except Exception:      # noqa: BLE001 — an empty body is a valid request
            pass
        # Confirmation is required in the payload as well as by the admin check:
        # this is not something to trigger by following a link.
        if not bool(body.get("confirm")):
            return JSONResponse(
                {"error": "confirm=true is required — review /api/update/status first"},
                status_code=400)
        result = _up.apply(backup=bool(body.get("backup", True)),
                           force=bool(body.get("force", False)))
        _audit.log_event(user["id"], user["username"],
                         "update.applied" if result["ok"] else "update.refused",
                         detail=f"{result.get('from','')}->{result.get('to','')} "
                                f"{result.get('message','')}"[:100])
        return JSONResponse({"result": result})

    @app.post("/api/update/rollback")
    async def update_rollback_api(request: Request, user: dict = Depends(require_admin)):
        from .. import updater as _up
        body = await request.json()
        commit = str(body.get("commit") or "").strip()
        result = _up.rollback(commit)
        _audit.log_event(user["id"], user["username"], "update.rollback",
                         detail=f"{commit} {result.get('message','')}"[:100])
        return JSONResponse({"result": result})

    # ── Agents ─────────────────────────────────────────────────────────────
    # Any authenticated user may author a profile. That is safe because a profile
    # can only ever NARROW what its invoker can reach (suni.agents.effective_grants)
    # — authoring one grants the author nothing they did not already have.

    @app.get("/api/agents")
    async def agents_list_api(user: dict = Depends(get_current_user)):
        from .. import agents as _agents
        role = user.get("role", "standard")
        rows = _agents.list_for_user(user["id"], role)
        # Report the EFFECTIVE grants, not the declared ones: what an agent can do
        # differs by who is asking, and showing the file's wish list would tell a
        # restricted user their agent has reach it will not actually get.
        prefixes = getattr(getattr(orchestrator, "registry", None), "_mcp_prefixes", [])
        for r in rows:
            g = _agents.effective_grants(r, role, prefixes)
            r["effective"] = {
                "tools": "all" if g["allowed_tools"] is None else len(g["allowed_tools"]),
                "mcp": "all" if g["mcp_prefixes"] is None else g["mcp_prefixes"],
                "mode": g["mode"],
                "model": g["model"] or "default",
            }
        return JSONResponse({"agents": rows})

    @app.post("/api/agents")
    async def agents_create_api(request: Request, user: dict = Depends(get_current_user)):
        from .. import agents as _agents
        body = await request.json()
        name = str(body.get("name") or "").strip()
        prompt = str(body.get("system_prompt") or "").strip()
        if not name or not prompt:
            return JSONResponse({"error": "name and system_prompt are required"},
                                status_code=400)
        rec = _agents.create(
            name=name,
            system_prompt=prompt,
            owner_id=user["id"],
            description=str(body.get("description") or ""),
            model=str(body.get("model") or ""),
            mode=str(body.get("mode") or "assistant"),
            tools=body.get("tools"),
            blocked=body.get("blocked") or [],
            mcp_servers=body.get("mcp_servers"),
            max_steps=int(body.get("max_steps") or 0),
            max_runs_day=int(body.get("max_runs_day") or 0),
        )
        _audit.log_event(user["id"], user["username"], "agent.created",
                         detail=f"name={name}", target_id=rec["slug"],
                         agent_slug=rec["slug"])
        return JSONResponse({"agent": rec})

    @app.get("/api/agents/{slug}")
    async def agents_get_api(slug: str, user: dict = Depends(get_current_user)):
        from .. import agents as _agents
        role = user.get("role", "standard")
        if slug not in {a["slug"] for a in _agents.list_for_user(user["id"], role)}:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"agent": _agents.get(slug)})

    @app.patch("/api/agents/{slug}")
    async def agents_update_api(slug: str, request: Request,
                                user: dict = Depends(get_current_user)):
        from .. import agents as _agents
        body = await request.json()
        rec = _agents.update(slug, user["id"], user.get("role", ""),
                             **{k: v for k, v in body.items()
                                if k in ("name", "description", "model", "mode",
                                         "tools", "blocked", "mcp_servers",
                                         "enabled", "system_prompt")})
        if rec is None:
            return JSONResponse({"error": "not found or not yours"}, status_code=403)
        _audit.log_event(user["id"], user["username"], "agent.updated",
                         detail=",".join(sorted(body.keys()))[:100],
                         target_id=slug, agent_slug=slug)
        return JSONResponse({"agent": rec})

    @app.post("/api/agents/{slug}/dry-run")
    async def agents_dry_run_api(slug: str, request: Request,
                                 user: dict = Depends(get_current_user)):
        """What would this agent do with this task — without doing any of it."""
        from .. import agents as _agents
        from ..core.context import Context as _Ctx
        body = await request.json()
        task = str(body.get("task") or "").strip()
        if not task:
            return JSONResponse({"error": "task is required"}, status_code=400)
        role = user.get("role", "standard")
        if slug not in {a["slug"] for a in _agents.list_for_user(user["id"], role)}:
            return JSONResponse({"error": "not found"}, status_code=404)
        profile = _agents.get(slug)
        if not profile:
            return JSONResponse({"error": "not found"}, status_code=404)
        profile["_username"] = user["username"]
        preview = await orchestrator._safe_run(
            task, _Ctx(), memory_override=_get_user_memory(user["id"]),
            user_role=role, user_id=user["id"], agent_profile=profile, dry_run=True)
        _audit.log_event(user["id"], user["username"], "agent.dry_run",
                         detail=task[:100], target_id=slug, agent_slug=slug)
        return JSONResponse({"preview": preview})

    @app.get("/api/agents/{slug}/report")
    async def agents_report_api(slug: str, days: int = 7,
                                user: dict = Depends(get_current_user)):
        from .. import agents as _agents
        if slug not in {a["slug"] for a in _agents.list_for_user(user["id"], user.get("role", ""))}:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"report": _agents.report(slug, max(1, min(int(days), 365)))})

    @app.get("/api/agents/{slug}/members")
    async def agents_members_api(slug: str, user: dict = Depends(get_current_user)):
        from .. import agents as _agents
        if slug not in {a["slug"] for a in _agents.list_for_user(user["id"], user.get("role", ""))}:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"members": _agents.members(slug)})

    @app.post("/api/agents/{slug}/share")
    async def agents_share_api(slug: str, request: Request,
                               user: dict = Depends(get_current_user)):
        from .. import agents as _agents
        body = await request.json()
        target = str(body.get("user_id") or "").strip()
        role = str(body.get("role") or "viewer").strip()
        if not target:
            return JSONResponse({"error": "user_id is required"}, status_code=400)
        if not _agents.share(slug, target, role, user["id"], user.get("role", "")):
            return JSONResponse({"error": "not permitted, or role must be viewer/editor"},
                                status_code=403)
        _audit.log_event(user["id"], user["username"], "agent.shared",
                         detail=f"{target} as {role}", target_id=slug, agent_slug=slug)
        return JSONResponse({"ok": True})

    @app.delete("/api/agents/{slug}/share/{target_id}")
    async def agents_unshare_api(slug: str, target_id: str,
                                 user: dict = Depends(get_current_user)):
        from .. import agents as _agents
        if not _agents.unshare(slug, target_id, user["id"], user.get("role", "")):
            return JSONResponse({"error": "not permitted (the owner cannot be removed)"},
                                status_code=403)
        _audit.log_event(user["id"], user["username"], "agent.unshared",
                         detail=target_id, target_id=slug, agent_slug=slug)
        return JSONResponse({"ok": True})

    @app.delete("/api/agents/{slug}")
    async def agents_delete_api(slug: str, user: dict = Depends(get_current_user)):
        from .. import agents as _agents
        role = user.get("role", "standard")
        if not _agents.delete(slug, user["id"], role):
            return JSONResponse({"error": "not found or not yours"}, status_code=403)
        _audit.log_event(user["id"], user["username"], "agent.deleted",
                         target_id=slug, agent_slug=slug)
        return JSONResponse({"ok": True})

    # ── Schedules ──────────────────────────────────────────────────────────

    @app.get("/api/schedules")
    async def schedules_list_api(user: dict = Depends(get_current_user)):
        from .. import schedules as _s
        return JSONResponse({"schedules": _s.list_for_user(user["id"], user.get("role", ""))})

    @app.post("/api/schedules")
    async def schedules_create_api(request: Request, user: dict = Depends(get_current_user)):
        from .. import schedules as _s
        from .. import agents as _agents
        body = await request.json()
        name = str(body.get("name") or "").strip()
        prompt = str(body.get("prompt") or "").strip()
        cadence = str(body.get("cadence") or "").strip()
        if not (name and prompt and cadence):
            return JSONResponse({"error": "name, prompt and cadence are required"},
                                status_code=400)

        # An agent may only be scheduled by someone who can already use it —
        # otherwise a schedule would be a way to invoke agents you cannot reach.
        slug = str(body.get("agent") or "").strip()
        if slug:
            visible = {a["slug"] for a in _agents.list_for_user(user["id"], user.get("role", ""))}
            if slug not in visible:
                return JSONResponse({"error": "unknown agent"}, status_code=403)

        # Delivery is fixed here, by a human. The model never picks a recipient.
        delivery = body.get("delivery") or {}
        if delivery.get("type") == "email" and not str(delivery.get("to") or "").strip():
            return JSONResponse({"error": "email delivery needs a 'to' address"},
                                status_code=400)
        try:
            rec = _s.create(name=name, prompt=prompt, cadence=cadence,
                            owner_id=user["id"], owner_name=user["username"],
                            agent_slug=slug, delivery=delivery)
        except _s.CadenceError as exc:
            # Refuse rather than substitute a cadence the user did not ask for.
            return JSONResponse({"error": str(exc)}, status_code=400)
        _audit.log_event(user["id"], user["username"], "schedule.created",
                         detail=f"{name} [{cadence}]", target_id=rec["id"],
                         agent_slug=slug)
        return JSONResponse({"schedule": rec})

    @app.delete("/api/schedules/{sched_id}")
    async def schedules_delete_api(sched_id: str, user: dict = Depends(get_current_user)):
        from .. import schedules as _s
        if not _s.delete(sched_id, user["id"], user.get("role", "")):
            return JSONResponse({"error": "not found or not yours"}, status_code=403)
        _audit.log_event(user["id"], user["username"], "schedule.deleted", target_id=sched_id)
        return JSONResponse({"ok": True})

    @app.post("/api/schedules/{sched_id}/enabled")
    async def schedules_toggle_api(sched_id: str, request: Request,
                                   user: dict = Depends(get_current_user)):
        from .. import schedules as _s
        body = await request.json()
        ok = _s.set_enabled(sched_id, bool(body.get("enabled", True)),
                            user["id"], user.get("role", ""))
        if not ok:
            return JSONResponse({"error": "not found or not yours"}, status_code=403)
        return JSONResponse({"ok": True})

    # ── Projects ───────────────────────────────────────────────────────────

    @app.get("/api/projects")
    async def projects_list_api(
        status: str = "active",
        user: dict = Depends(get_current_user),
    ):
        from .. import projects as _p
        filter_status = "" if status == "all" else status
        rows = _p.list_projects(user_id=user["id"], status=filter_status)
        return JSONResponse({"projects": rows})

    @app.post("/api/projects")
    async def projects_create_api(request: Request, user: dict = Depends(get_current_user)):
        from .. import projects as _p
        body = await request.json()
        name = str(body.get("name", "")).strip()
        if not name:
            raise HTTPException(400, "name required")
        proj = _p.create_project(
            name=name,
            goal=str(body.get("goal", "")),
            user_id=user["id"],
        )
        return JSONResponse(proj, status_code=201)

    @app.get("/api/projects/{project_id}")
    async def projects_get_api(project_id: str, user: dict = Depends(get_current_user)):
        from .. import projects as _p
        is_admin = user.get("role") == "admin"
        if not _p.can_access(project_id, user["id"], is_admin=is_admin):
            raise HTTPException(404, "Project not found")
        proj = _p.get_project(project_id, requesting_user_id=user["id"])
        return JSONResponse(proj)

    @app.put("/api/projects/{project_id}")
    async def projects_update_api(
        project_id: str, request: Request, user: dict = Depends(get_current_user)
    ):
        from .. import projects as _p
        is_admin = user.get("role") == "admin"
        if not _p.can_access(project_id, user["id"], is_admin=is_admin, min_role="editor"):
            raise HTTPException(404, "Project not found")
        body = await request.json()
        updated = _p.update_project(
            project_id,
            name=body.get("name"),
            goal=body.get("goal"),
            status=body.get("status"),
        )
        return JSONResponse(updated)

    @app.post("/api/projects/{project_id}/log")
    async def projects_log_api(
        project_id: str, request: Request, user: dict = Depends(get_current_user)
    ):
        from .. import projects as _p
        body   = await request.json()
        action = str(body.get("action", "")).strip()
        if not action:
            raise HTTPException(400, "action required")
        is_admin = user.get("role") == "admin"
        if not _p.can_access(project_id, user["id"], is_admin=is_admin, min_role="editor"):
            raise HTTPException(404, "Project not found")
        updated = _p.add_action(project_id, action, by=user["username"])
        return JSONResponse(updated)

    @app.post("/api/projects/{project_id}/files")
    async def projects_link_file_api(
        project_id: str, request: Request, user: dict = Depends(get_current_user)
    ):
        from .. import projects as _p
        body = await request.json()
        path = str(body.get("path", "")).strip()
        if not path:
            raise HTTPException(400, "path required")
        is_admin = user.get("role") == "admin"
        if not _p.can_access(project_id, user["id"], is_admin=is_admin, min_role="editor"):
            raise HTTPException(404, "Project not found")
        updated = _p.link_file(project_id, path)
        return JSONResponse(updated)

    @app.post("/api/projects/{project_id}/contacts")
    async def projects_link_contact_api(
        project_id: str, request: Request, user: dict = Depends(get_current_user)
    ):
        from .. import projects as _p
        body       = await request.json()
        contact_id = str(body.get("contact_id", "")).strip()
        if not contact_id:
            raise HTTPException(400, "contact_id required")
        is_admin = user.get("role") == "admin"
        if not _p.can_access(project_id, user["id"], is_admin=is_admin, min_role="editor"):
            raise HTTPException(404, "Project not found")
        updated = _p.link_contact(project_id, contact_id)
        return JSONResponse(updated)

    @app.delete("/api/projects/{project_id}")
    async def projects_delete_api(project_id: str, user: dict = Depends(get_current_user)):
        from .. import projects as _p
        ok = _p.delete_project(project_id, user["id"])
        if not ok:
            raise HTTPException(404, "Project not found or insufficient permissions")
        return JSONResponse({"ok": True})

    @app.get("/api/projects/{project_id}/members")
    async def projects_members_list(project_id: str, user: dict = Depends(get_current_user)):
        from .. import projects as _p
        is_admin = user.get("role") == "admin"
        if not _p.can_access(project_id, user["id"], is_admin=is_admin):
            raise HTTPException(404, "Project not found")
        members = _p.get_members(project_id)
        return JSONResponse({"members": members})

    @app.post("/api/projects/{project_id}/members")
    async def projects_members_add(
        project_id: str, request: Request, user: dict = Depends(get_current_user)
    ):
        from .. import projects as _p
        from .. import auth as _auth
        if not _p.can_access(project_id, user["id"], min_role="owner"):
            raise HTTPException(403, "Only the project owner can add members")
        body     = await request.json()
        username = str(body.get("username", "")).strip()
        role     = str(body.get("role", "editor")).lower()
        if not username:
            raise HTTPException(400, "username required")
        if role not in ("owner", "editor", "viewer"):
            raise HTTPException(400, "role must be owner, editor, or viewer")
        target = _auth.get_user_by_username(username)
        if not target:
            raise HTTPException(404, f"User '{username}' not found")
        ok = _p.add_member(project_id, target["id"], role=role)
        if not ok:
            raise HTTPException(404, "Project not found")
        members = _p.get_members(project_id)
        return JSONResponse({"members": members}, status_code=201)

    @app.delete("/api/projects/{project_id}/members/{target_user_id}")
    async def projects_members_remove(
        project_id: str, target_user_id: str, user: dict = Depends(get_current_user)
    ):
        from .. import projects as _p
        # Owner can remove anyone; members can remove themselves
        if target_user_id != user["id"]:
            if not _p.can_access(project_id, user["id"], min_role="owner"):
                raise HTTPException(403, "Only the project owner can remove other members")
        ok = _p.remove_member(project_id, target_user_id)
        if not ok:
            raise HTTPException(409, "Cannot remove the last owner or member not found")
        return JSONResponse({"ok": True})

    # ── Background Task Queue ─────────────────────────────────────────────

    @app.get("/api/tasks")
    async def tasks_list_api(
        status: str = "all",
        user: dict = Depends(get_current_user),
    ):
        from .. import task_queue as _tq
        tasks = _tq.list_tasks(user_id=user["id"])
        if status != "all":
            tasks = [t for t in tasks if t["status"] == status]
        return JSONResponse({"tasks": tasks})

    @app.get("/api/tasks/{task_id}")
    async def tasks_get_api(task_id: str, user: dict = Depends(get_current_user)):
        from .. import task_queue as _tq
        task = _tq.get_task(task_id)
        if not task or (task["user_id"] != user["id"] and user.get("role") != "admin"):
            raise HTTPException(404, "Task not found")
        return JSONResponse(task)

    @app.delete("/api/tasks/{task_id}")
    async def tasks_cancel_api(task_id: str, user: dict = Depends(get_current_user)):
        from .. import task_queue as _tq
        ok = _tq.cancel_task(task_id, user["id"])
        if not ok:
            raise HTTPException(404, "Task not found or cannot be cancelled")
        return JSONResponse({"ok": True})

    @app.get("/api/tasks/{task_id}/progress")
    async def tasks_progress_sse(task_id: str, user: dict = Depends(get_current_user)):
        """SSE stream for a specific background task's progress events."""
        from .. import task_queue as _tq
        task = _tq.get_task(task_id)
        if not task or (task["user_id"] != user["id"] and user.get("role") != "admin"):
            raise HTTPException(404, "Task not found")

        async def generate():
            q = _tq._task_queue(task_id)
            yield f"data: {json.dumps({'type': 'task_state', 'task': task})}\n\n"
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps(evt)}\n\n"
                    if evt.get("type") in ("task_done", "task_failed", "task_cancelled"):
                        break
                except asyncio.TimeoutError:
                    # Heartbeat
                    t = _tq.get_task(task_id) or {}
                    if t.get("status") in ("done", "failed", "cancelled"):
                        yield f"data: {json.dumps({'type': 'task_done', 'task_id': task_id})}\n\n"
                        break
                    yield "data: {\"type\": \"heartbeat\"}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── Daily Briefing ────────────────────────────────────────────────────

    @app.get("/api/briefing")
    async def briefing_get(user: dict = Depends(get_current_user)):
        """Compose and return the current briefing (does not push)."""
        from .. import briefing as _b
        text = await _b.compose_briefing()
        return JSONResponse({"briefing": text})

    @app.post("/api/briefing/send")
    async def briefing_send(
        background_tasks: BackgroundTasks,
        user: dict = Depends(get_current_user),
    ):
        """Compose and push the briefing now via all configured channels."""
        from .. import briefing as _b
        async def _run():
            text = await _b.compose_briefing()
            sent = await _b.push_briefing(text)
            _log.info("[BRIEFING] manual send by %s via %s", user["username"], sent)
        background_tasks.add_task(_run)
        return JSONResponse({"ok": True, "message": "Briefing sending in background"})

    # -----------------------------------------------------------------------
    # Dashboard API
    # -----------------------------------------------------------------------

    async def _metrics_collector(stop: asyncio.Event) -> None:
        """Sample GPU + Ollama stats every 30 s and append to the history ring buffer."""
        while not stop.is_set():
            try:
                gpu, ps = await asyncio.gather(_gpu_stats(), _ollama_ps())
                sample = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "gpu": gpu,
                    "model_loaded": bool(ps),
                    "ctx_len": ps[0].get("context_length") if ps else None,
                    "req_count": len(activity_log),
                }
                _METRICS_HISTORY.append(sample)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=_METRICS_INTERVAL)
                break
            except asyncio.TimeoutError:
                pass

    async def _gpu_stats() -> list[dict]:
        try:
            loop = asyncio.get_event_loop()
            def _run():
                r = subprocess.run(
                    ["nvidia-smi",
                     "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=3
                )
                out = []
                for line in r.stdout.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) == 5:
                        out.append({
                            "name": parts[0],
                            "mem_used": int(parts[1]),
                            "mem_total": int(parts[2]),
                            "util_pct": int(parts[3]),
                            "temp_c": int(parts[4]),
                        })
                return out
            return await loop.run_in_executor(None, _run)
        except Exception:
            return []

    async def _ollama_ps() -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(f"{suni_config.ollama_host()}/api/ps")
                data = r.json()
                return data.get("models", [])
        except Exception:
            return []

    async def _ollama_models() -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(f"{suni_config.ollama_host()}/api/tags")
                return [m["name"] for m in r.json().get("models", [])]
        except Exception:
            return []

    @app.get("/api/dashboard", dependencies=[Depends(_check_token)])
    async def dashboard():
        gpu, ollama_ps, mem_stats = await asyncio.gather(
            _gpu_stats(), _ollama_ps(), asyncio.to_thread(memory.stats)
        )
        uptime_s = int(time.time() - _START_TIME)
        tools_all = registry.names()
        recent_tools = []
        for entry in reversed(list(activity_log)):
            for t in entry.get("tools", []):
                for name in t.split(", "):
                    if name and name not in recent_tools:
                        recent_tools.append(name)
            if len(recent_tools) >= 10:
                break

        from .. import system_profile as _sp
        return JSONResponse({
            "uptime_s":    uptime_s,
            "system": {
                "ram_gb":    round(_sp.RAM_GB),
                "vram_mb":   _sp.VRAM_MB,
                "cpu_cores": _sp.CPU_CORES,
                "max_doc_mb":   _sp.MAX_DOC_FILE_MB,
                "embed_batch":  _sp.EMBED_BATCH_SIZE,
                "ingest_concurrency": _sp.INGEST_CONCURRENCY,
                "num_ctx":   _sp.NUM_CTX,
                "num_history": _sp.NUM_HISTORY,
                "compress_at": _sp.COMPRESS_THRESHOLD,
            },
            "gpu":         gpu,
            "ollama_ps":   [
                {
                    "name":           m.get("name"),
                    "size_vram_mb":   round(m.get("size_vram", 0) / 1_048_576),
                    "context_length": m.get("context_length"),
                    "expires_at":     m.get("expires_at"),
                }
                for m in ollama_ps
            ],
            "memory": {
                "total":      mem_stats.get("total", 0),
                "active":     mem_stats.get("active", mem_stats.get("total", 0)),
                "deprecated": mem_stats.get("deprecated", 0),
                "recent":     mem_stats.get("recent", []),
            },
            "tools": {
                "all":    tools_all,
                "recent": recent_tools,
                "mcp_prefixes": getattr(registry, "_mcp_prefixes", []),
            },
            "mcp_servers": [
                {"name": name, "connected": True}
                for name in bridge._connections.keys()
            ],
            "activity": list(activity_log),
            "config":   suni_config.all(),
        })

    @app.get("/api/knowledge/status", dependencies=[Depends(_check_token)])
    async def knowledge_status():
        return JSONResponse({
            "doc_store":  doc_store.stats(),
            "scanner":    scanner_status(),
            "config": {
                "paths":    suni_config.get("doc_paths", []),
                "interval": suni_config.get("doc_scan_interval", 600),
            },
        })

    @app.post("/api/knowledge/scan", dependencies=[Depends(_check_token)])
    @_limiter.limit("2/5minutes")
    async def knowledge_scan_now(request: Request):
        """Trigger an immediate scan in the background."""
        from ..ingestion.document_scanner import scan_once
        paths = suni_config.get("doc_paths", [])
        if not paths:
            return JSONResponse({"ok": False, "error": "No paths configured"})
        asyncio.create_task(scan_once(paths, doc_store, memory.embed_batch_sync))
        return JSONResponse({"ok": True, "message": "Scan started"})

    @app.get("/api/metrics/history", dependencies=[Depends(_check_token)])
    async def metrics_history():
        return JSONResponse({"history": list(_METRICS_HISTORY)})

    # -----------------------------------------------------------------------
    # Benchmarks API — the 33-metric dashboard (live telemetry + on-demand suites)
    # -----------------------------------------------------------------------
    from ..benchmarks import dashboard as _bench_dash
    from ..benchmarks import runner as _bench_runner
    from ..benchmarks import store as _bench_store
    from ..benchmarks import metrics as _bench_metrics
    from ..system_profile import NUM_CTX as _BENCH_NUM_CTX

    _OLLAMA_HOST = suni_config.ollama_host()

    @app.get("/api/benchmarks/metrics", dependencies=[Depends(_check_token)])
    async def benchmarks_metrics():
        kwh = suni_config.get("kwh_price", None)
        payload = await _bench_dash.build_payload(
            SUNI_MODEL, _OLLAMA_HOST, _BENCH_NUM_CTX, kwh_price=kwh
        )
        return JSONResponse(payload)

    @app.get("/api/benchmarks/suites", dependencies=[Depends(_check_token)])
    async def benchmarks_suites():
        out = [
            {"key": k, "metrics": _bench_metrics.suite_metric_ids(k)}
            for k in _bench_metrics.all_suite_keys()
        ]
        return JSONResponse({"suites": out})

    @app.get("/api/benchmarks/progress", dependencies=[Depends(_check_token)])
    async def benchmarks_progress():
        return JSONResponse(_bench_runner.progress())

    @app.get("/api/benchmarks/runs", dependencies=[Depends(_check_token)])
    async def benchmarks_runs():
        return JSONResponse({"runs": _bench_store.list_runs()})

    @app.get("/api/benchmarks/runs/{run_id}", dependencies=[Depends(_check_token)])
    async def benchmarks_run_detail(run_id: str):
        rec = _bench_store.get_run(run_id)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(rec)

    @app.post("/api/benchmarks/run")
    async def benchmarks_run(request: Request, admin: dict = Depends(require_admin)):
        if _bench_runner.is_running():
            return JSONResponse(
                {"ok": False, "error": "A benchmark run is already in progress."},
                status_code=409,
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        suites = body.get("suites") or None
        limit = body.get("limit")
        tools = []
        try:
            tools = registry.get_ollama_tools()
        except Exception:
            pass

        async def _bg():
            try:
                await _bench_runner.run(
                    model=SUNI_MODEL, host=_OLLAMA_HOST, num_ctx=_BENCH_NUM_CTX,
                    suite_keys=suites, limit=limit, registry_tools=tools,
                )
            except Exception as e:
                _log.error("[BENCH] background run failed: %s", e)

        asyncio.create_task(_bg())
        _log.info("[BENCH] run requested by %s (suites=%s)", admin["username"], suites or "all")
        return JSONResponse({
            "ok": True,
            "message": "Benchmark started. SUNI will respond more slowly while it runs.",
        })

    @app.get("/api/config", dependencies=[Depends(_check_token)])
    async def get_config():
        # Never echo backend credentials — /api/config is read by every
        # authenticated user's chat page, not just admins. Blank out *_api_key
        # (the admin re-enters to change; blank-on-save preserves the stored value).
        cfg = suni_config.all()
        for k in list(cfg.keys()):
            if k.endswith("_api_key"):
                cfg[k] = ""
        # Telegram/Discord bot tokens are secrets too (don't match *_api_key suffix).
        cfg["has_telegram_token"] = bool(str(cfg.get("telegram_bot_token", "")).strip())
        cfg["telegram_bot_token"] = ""
        cfg["has_discord_token"] = bool(str(cfg.get("discord_bot_token", "")).strip())
        cfg["discord_bot_token"] = ""
        cfg["has_slack_app_token"] = bool(str(cfg.get("slack_app_token", "")).strip())
        cfg["slack_app_token"] = ""
        cfg["has_slack_bot_token"] = bool(str(cfg.get("slack_bot_token", "")).strip())
        cfg["slack_bot_token"] = ""
        # The SMTP password is a secret on the same footing as the bot tokens.
        cfg["has_smtp_pass"] = bool(str(cfg.get("smtp_pass", "")).strip())
        cfg["smtp_pass"] = ""
        # Log-shipping credentials. Same footing again: a collector token or an
        # SFTP password is exactly as sensitive as the mail password, and this
        # one guards a stream containing IPs, usernames and query previews.
        for _sk in ("logship_token", "logship_password"):
            cfg[f"has_{_sk}"] = bool(str(cfg.get(_sk, "")).strip())
            cfg[_sk] = ""
        # Mask per-entry API keys in the model chain — never echo secrets. Expose
        # only a has_key flag so the admin UI can show "configured" without the key.
        for _lk in ("model_chain", "collaborate_pool"):
            if isinstance(cfg.get(_lk), list):
                cfg[_lk] = [
                    {**{k: v for k, v in e.items() if k != "api_key"},
                     "has_key": bool(str(e.get("api_key", "")).strip())}
                    for e in cfg[_lk] if isinstance(e, dict)
                ]
        return JSONResponse(cfg)

    @app.post("/api/config", dependencies=[Depends(_check_token)])
    async def post_config(request: Request):
        body = await request.json()
        # Validate types loosely
        allowed = set(suni_config.DEFAULTS.keys())
        filtered = {k: v for k, v in body.items() if k in allowed}
        # Blank *_api_key means "keep the stored value" (it was redacted on GET) —
        # so an unchanged form never wipes a configured key.
        for k in list(filtered.keys()):
            if k.endswith("_api_key") and not str(filtered[k]).strip():
                del filtered[k]
        # Same blank-preserves-stored rule for the Telegram/Discord bot tokens.
        if "telegram_bot_token" in filtered and not str(filtered["telegram_bot_token"]).strip():
            del filtered["telegram_bot_token"]
        if "discord_bot_token" in filtered and not str(filtered["discord_bot_token"]).strip():
            del filtered["discord_bot_token"]
        for _sk in ("slack_app_token", "slack_bot_token", "smtp_pass",
                    "logship_token", "logship_password"):
            if _sk in filtered and not str(filtered[_sk]).strip():
                del filtered[_sk]
        # Model chain / collaboration pool: preserve each entry's stored api_key
        # when the incoming one is blank (GET masked it), matched by entry id;
        # drop the UI-only has_key.
        for _lk in ("model_chain", "collaborate_pool"):
            if isinstance(filtered.get(_lk), list):
                _stored = {e.get("id"): e for e in (suni_config.all().get(_lk) or [])
                           if isinstance(e, dict)}
                _clean = []
                for e in filtered[_lk]:
                    if not isinstance(e, dict):
                        continue
                    e = {k: v for k, v in e.items() if k != "has_key"}
                    if not str(e.get("api_key", "")).strip():
                        e["api_key"] = (_stored.get(e.get("id")) or {}).get("api_key", "")
                    _clean.append(e)
                filtered[_lk] = _clean
        # Merge over the CURRENT config, not defaults — a PARTIAL POST (e.g. a
        # single backend field) must preserve every other setting rather than
        # resetting it. (config.save() itself merges over DEFAULTS.)
        merged = suni_config.all()
        merged.update(filtered)
        suni_config.save(merged)
        # Apply live-applicable settings immediately
        if "num_ctx" in filtered:
            orchestrator.primary.num_ctx = int(filtered["num_ctx"])
        if "model" in filtered:
            orchestrator.primary.model = filtered["model"]
        # Runtime backend switch (Ollama ⇄ vLLM). Build the new agents, then
        # atomically MUTATE the live orchestrator in place — never rebind the
        # variable (silent no-op risk) or drop the registry (loses MCP tools).
        # In-flight requests finish on the old agent; new ones use the new one.
        if {"vllm_base_url", "vllm_model", "vllm_api_key",
            "model_chain_routing", "model_chain"} & set(filtered.keys()):
            try:
                from ..models import factory as _factory
                new_primary, new_tiers = _make_backend_agents(
                    getattr(orchestrator, "_last_tier_map", None))
                orchestrator.primary = new_primary
                orchestrator._tier_agents = new_tiers
                _log.info("[BACKEND] runtime switch → %s (by %s)",
                          _factory.active_backend(),
                          "admin")
            except Exception as e:
                _log.error("[BACKEND] runtime switch failed: %s", e, exc_info=True)
                raise HTTPException(500, f"Backend switch failed: {e}")
        # Live channel start/stop/restart — reconcile any channel whose enable
        # flag or token(s) changed, so the admin never needs to restart SUNI.
        _touched = set(filtered.keys())
        for _cname, (_r, _d, _c, _keys) in _CHANNELS.items():
            if _keys & _touched:
                try:
                    await _reconcile_channel(_cname)
                    _log.info("[CHANNEL] %s reconciled after config change", _cname)
                except Exception as e:
                    _log.error("[CHANNEL] %s reconcile failed: %s", _cname, e)
        return JSONResponse({"ok": True, "saved": list(filtered.keys())})

    @app.get("/api/logs", dependencies=[Depends(_check_token)])
    async def get_logs(lines: int = 200, level: str = "ALL", date: str = ""):
        import re as _re
        from ..logger import current_log_path
        if date and not _re.match(r'^\d{4}-\d{2}-\d{2}$', date):
            return JSONResponse({"error": "Invalid date — use YYYY-MM-DD"}, status_code=400)
        log_file = Path("logs") / f"suni_{date}.log" if date else current_log_path()
        if not log_file.exists():
            return JSONResponse({"lines": [], "file": str(log_file)})
        try:
            all_lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            if level != "ALL":
                all_lines = [l for l in all_lines if f"| {level}" in l]
            tail = all_lines[-lines:]
            return JSONResponse({"lines": tail, "total": len(all_lines), "file": str(log_file)})
        except Exception as e:
            return JSONResponse({"lines": [str(e)], "file": str(log_file)})

    @app.get("/api/ollama/models", dependencies=[Depends(_check_token)])
    async def ollama_models():
        return JSONResponse({"models": await _ollama_models()})

    @app.get("/api/tts/voices", dependencies=[Depends(_check_token)])
    async def tts_voices():
        return JSONResponse({"voices": list(EDGE_TTS_VOICES)})

    return app



