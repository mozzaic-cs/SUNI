from __future__ import annotations
import asyncio
import re
import time
from collections import deque
from datetime import datetime, timezone
from rich.console import Console
from rich.table import Table
from .base_agent import BaseAgent
from .message import Message, Role
from .context import Context
from .router import TaskRouter
from .compressor import ContextCompressor, estimate_tokens
from .model_tier import complexity_score, needs_escalation, CLAUDE_CODE_TIER, direct_to_claude_code
from ..tools.registry import ToolRegistry, USER_ID_CTX
from ..logger import get_logger
from .. import rbac as _rbac
from .. import config as _cfg
from .. import approval as _approval
from .. import output_guard as _output_guard
from .. import policy as _policy
from .. import vision as _vision
from ..models import health as _bhealth
from ..system_profile import MAX_LOCAL_TIER, DEFAULT_TIER
from ..benchmarks import telemetry as _bench_telemetry

# BCP-47 → (human name, native instruction)
_LANG_MAP: dict[str, tuple[str, str]] = {
    "pt-PT": ("European Portuguese",
              "Responde SEMPRE em Português Europeu (Portugal). "
              "Mantém a tua personalidade, humor seco e ausência de pontos de exclamação. "
              "Usa vocabulário e ortografia de Portugal, não do Brasil."),
    "pt-BR": ("Brazilian Portuguese",
              "Responda SEMPRE em Português do Brasil. "
              "Mantenha sua personalidade e estilo habitual."),
    "es-ES": ("Spanish",
              "Responde SIEMPRE en español. Mantén tu personalidad y estilo habitual."),
    "fr-FR": ("French",
              "Réponds TOUJOURS en français. Conserve ta personnalité habituelle."),
    "de-DE": ("German",
              "Antworte IMMER auf Deutsch. Behalte deine übliche Persönlichkeit."),
}


# Replies SUNI composes in Python rather than generating. The language
# instruction only reaches the model, so any hardcoded sentence answers a
# pt-PT user in English — which is exactly how "create a PDF about Coimbra"
# came back in English to a Portuguese-configured account.
#
# English is the fallback for a language with no entry, so an unsupported
# locale degrades to English rather than to a missing key.
_CANNED: dict[str, dict[str, str]] = {
    "no_email_address": {
        "en": "I could not find an email address in your request. "
              "Please specify who to send it to.",
        "pt-PT": "Não encontrei um endereço de email no seu pedido. "
                 "Indique para quem devo enviar.",
        "pt-BR": "Não encontrei um endereço de e-mail no seu pedido. "
                 "Indique para quem devo enviar.",
        "es-ES": "No encontré una dirección de correo en su solicitud. "
                 "Indique a quién debo enviarlo.",
    },
    "plan_cancelled": {
        "en": "Plan cancelled. What would you like to do instead?",
        "pt-PT": "Plano cancelado. O que prefere fazer em vez disso?",
        "pt-BR": "Plano cancelado. O que você prefere fazer no lugar?",
        "es-ES": "Plan cancelado. ¿Qué prefiere hacer en su lugar?",
    },
    "image_failed": {
        "en": "I couldn't analyze the image due to an unexpected error.",
        "pt-PT": "Não consegui analisar a imagem devido a um erro inesperado.",
        "pt-BR": "Não consegui analisar a imagem devido a um erro inesperado.",
        "es-ES": "No pude analizar la imagen debido a un error inesperado.",
    },
    "no_compiled_content": {
        "en": "I could not find any compiled content to put into a PDF. "
              "Please compile the information first, then ask me to create the PDF.",
        "pt-PT": "Não encontrei conteúdo já compilado para colocar num PDF. "
                 "Compile primeiro a informação e depois peça-me o PDF.",
        "pt-BR": "Não encontrei conteúdo já compilado para colocar em um PDF. "
                 "Compile primeiro a informação e depois me peça o PDF.",
        "es-ES": "No encontré contenido ya compilado para poner en un PDF. "
                 "Compile primero la información y luego pídame el PDF.",
    },
    "attached": {
        "en": "Please find the requested information attached.",
        "pt-PT": "Segue em anexo a informação solicitada.",
        "pt-BR": "Segue em anexo a informação solicitada.",
        "es-ES": "Adjunto encontrará la información solicitada.",
    },
}


def _say(key: str, response_language: str = "") -> str:
    """A canned reply in the caller's language, falling back to English."""
    lang = (response_language or _cfg.get("response_language")
            or _cfg.get("stt_language", "en-GB") or "en")
    table = _CANNED.get(key, {})
    if lang in table:
        return table[lang]
    # A bare tag ("pt") should reach the regional entry ("pt-PT") rather than
    # falling through to English — a user configured as "pt" is not asking for
    # English. Exact match first so pt-BR never answers a pt-PT user.
    base = lang.split("-")[0]
    for tag, text in table.items():
        if tag.split("-")[0] == base:
            return text
    return table.get("en", "")


def _lang_instruction(response_language: str = "") -> str | None:
    """Return a language override instruction if not English, else None.

    response_language (per-user override) takes precedence over global config.
    """
    lang = response_language or _cfg.get("response_language") or _cfg.get("stt_language", "en-GB")
    if not lang or lang.startswith("en"):
        return None
    entry = _LANG_MAP.get(lang)
    if entry:
        return entry[1]
    return f"Always reply in the language with BCP-47 code '{lang}'."

_log = get_logger(__name__)
_NL = chr(10)      # newline as a constant, so hint-building needs no escapes

# Structured tool-selection (delegation, scheduling) needs at least a mid tier.
# Measured: at tier 2 the same prompt chose four different wrong tools across
# four runs. Below this, escalate rather than answer badly.
_MIN_TIER_FOR_STRUCTURED = 3

# An agent that declares tools needs a model that can call them rather than
# describe them. Same threshold, different reason: one is about choosing a
# tool, this is about emitting a well-formed call at all.
_MIN_TIER_FOR_TOOL_USE = 3

# How much prior assistant text counts as "something to put in a PDF".
_COMPILED_MIN_CHARS = 100


def _compiled_content(context) -> str:
    """The last substantial assistant message, or "" if there is none.

    Used by BOTH the direct-pdf dispatch guard and the handler. Keeping one
    definition is the point: when the guard and the handler each decided this
    for themselves, the guard admitted requests the handler then refused, and
    the refusal was a hardcoded English sentence telling the user to go and
    compile the text first.
    """
    for msg in reversed(getattr(context, "history", []) or []):
        if msg.role == Role.ASSISTANT and len(msg.content) > _COMPILED_MIN_CHARS:
            return msg.content
    return ""

# Global activity ring-buffer — readable by the dashboard API
activity_log: deque = deque(maxlen=20)

# Queries that require live data — pre-fetch web results before model runs
_WEB_RE = re.compile(
    r'\b(weather|forecast|temperature|rain|snow|wind|humidity|'
    r'news|headlines|current|today|tonight|tomorrow|'
    r'price|stock|crypto|bitcoin|score|result|standings|'
    r'who won|did .{1,30} win|is .{1,30} open|'
    r'happening|right now|at the moment|as of today)\b',
    re.IGNORECASE,
)

# Image-generation intent → the direct local-Stable-Diffusion path.
# Multilingual: English + Portuguese/Spanish/French verbs and nouns, so an image
# request always takes the single-shot direct path instead of the agent loop
# (which, on a local model, would unload itself while generating → thrash).
_IMAGE_GEN_RE = re.compile(
    r'\b(generate|create|make|draw|paint|render|produce|design|sketch|illustrate|'
    r'gerar|gera|criar|cria|desenhar|desenha|faz|fazer|fa[çc]a|produzir|ilustrar|pintar|'   # pt
    r'genera|crear|dibujar|dibuja|'                                                          # es
    r'g[ée]n[ée]rer|cr[ée]er|dessiner)\b'                                                    # fr
    r'[^.?!]{0,40}\b(image|images|picture|il+ustration|il+ustra[çc][ãa]o|drawing|artwork|'
    r'art|arte|photo|foto|fotografia|poster|cartaz|logo|graphic|gr[áa]fico|painting|pintura|'
    r'render|wallpaper|avatar|icon|[íi]cone|desenho|dibujo|retrato|quadro|imagem|imagens|imagen)\b',
    re.IGNORECASE,
)

# When the previous turn produced an image, these cues mark a follow-up as a
# refinement of that image (regenerate) rather than a brand-new request.
_IMAGE_REFINE_RE = re.compile(
    r'\b(referia|quero dizer|queria dizer|quis dizer|na verdade|'           # pt: clarify
    r'antes era|n[ãa]o era|melhor|maior|menor|torna[- ]?(a|o)?|'           # pt: edit/modify
    r'mais (realista|detalh|escur|claro|colorid|brilh)|'                   # pt: "more X"
    r'i meant|actually|rather than|instead|not that|make it|'              # en: clarify/edit
    r'more (realistic|detailed|colou?rful|vibrant)|bigger|smaller)',       # en: "more X"
    re.IGNORECASE,
)

# Queries about SUNI's own content — never need a web prefetch
_SKIP_PREFETCH_RE = re.compile(
    r'\b(article|articles|published|wrote|written|post|posts|'
    r'suniverse|you wrote|you published|your article|your post|'
    r'recent article|latest article|last article)\b',
    re.IGNORECASE,
)

console = Console(stderr=True)

MAX_TOOL_ITERATIONS = 8

# Strip accidentally echoed injected-context blocks from model output before
# sending to user. qwen2.5:7b sometimes echoes injected memory/doc context
# verbatim despite instructions.
_DOC_EXCERPT_RE = re.compile(
    r'\[(?:USER-)?DOC-EXCERPT-UNTRUSTED:[^\]]*\].*?\[/(?:USER-)?DOC-EXCERPT-UNTRUSTED\]'
    r'|\[MEMORY-UNTRUSTED\].*?\[/MEMORY-UNTRUSTED\]',
    re.DOTALL | re.IGNORECASE,
)


def _sanitize_response(text: str) -> str:
    """Remove any injected DOC-EXCERPT / MEMORY blocks the model echoed into output."""
    cleaned = _DOC_EXCERPT_RE.sub('', text)
    # Collapse runs of blank lines left by the removal
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


class Orchestrator:
    """
    Routes user input through the primary agent, handles tool calls,
    and delegates to sub-agents transparently.

    Optional memory_manager (MemoryManager) enables RAG: relevant memories
    are injected before each turn and the exchange is stored after.
    """

    def __init__(self, primary: BaseAgent, registry: ToolRegistry,
                 memory=None, skill_store=None,
                 tier_agents: dict[int, BaseAgent] | None = None):
        self.primary = primary
        self.registry = registry
        self.memory = memory            # MemoryManager | None
        self.skill_store = skill_store  # SkillStore | None
        self.sub_agents: dict[str, BaseAgent] = {}
        # tier_agents: {tier_number: OllamaAgent} — filled by server/main at startup
        # Tier 2 (default) always maps to self.primary for backwards compatibility.
        self._tier_agents: dict[int, BaseAgent] = tier_agents or {}
        if DEFAULT_TIER not in self._tier_agents:
            self._tier_agents[DEFAULT_TIER] = primary
        self._router = TaskRouter()
        self._compressor = ContextCompressor()
        self._pending_plans: dict = {}   # id(context) → list[ToolCall]

    def register_tier(self, tier: int, agent: BaseAgent) -> None:
        """Register an OllamaAgent for a specific tier number."""
        self._tier_agents[tier] = agent

    def _agent_for_model(self, model: str) -> BaseAgent | None:
        """Backend for a model named by an agent profile, built once and reused.

        Cached because a profile is invoked repeatedly and construction opens a
        client; keyed by model name, which is what the profile actually pins.
        """
        if not model:
            return None
        cache = getattr(self, "_model_agents", None)
        if cache is None:
            cache = self._model_agents = {}
        if model in cache:
            return cache[model]
        try:
            from ..models.factory import make_agent as _make_agent
            from ..main import resolve_system_prompt as _sys
            agent = _make_agent(name=f"profile:{model}", model=model, system_prompt=_sys())
        except Exception as exc:
            _log.warning("[AGENT] make_agent(%r) failed: %s", model, exc)
            return None
        cache[model] = agent
        return agent

    def _agent_for_tier(self, tier: int) -> BaseAgent | None:
        """Return the best available agent at or below the requested tier."""
        for t in range(tier, 0, -1):
            if t in self._tier_agents:
                return self._tier_agents[t]
        return self.primary

    def register_agent(self, agent: BaseAgent, description: str = "") -> None:
        """Register a sub-agent and expose it as a delegatable tool."""
        self.sub_agents[agent.name] = agent

        schema = {
            "name": f"delegate_to_{agent.name}",
            "description": (
                description
                or f"Delegate a task to the {agent.name} specialist agent. "
                f"Use this for tasks that {agent.name} handles better."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Full description of the task to delegate",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional extra context or constraints",
                    },
                },
                "required": ["task"],
            },
        }

        async def _delegate(task: str, context: str = "", _agent=agent) -> str:
            console.print(
                f"  [dim]→ delegating to [bold]{_agent.name}[/bold]...[/dim]"
            )
            msgs = []
            if context:
                msgs.append(Message(role=Role.SYSTEM, content=context, agent="orchestrator"))
            msgs.append(Message(role=Role.USER, content=task, agent="orchestrator"))
            result = await _agent.chat(msgs, Context())
            return result.content

        self.registry.register(schema, _delegate)

    async def run(
        self,
        user_input: str,
        context: Context,
        user_role: str = "admin",
        conv_mode: str = "assistant",
        response_language: str = "",
        user_mcp_servers: list[str] | None = None,
        event_cb=None,          # callable(dict) | None — receives tool/skill events
        user_id: str = "",      # for auto-config writes
        claude_api_key: str = "",  # per-user Anthropic key; '' = use global env var
        images: list[str] | None = None,  # paths of attached image files (vision path)
        agent_profile: dict | None = None,  # named agent profile; see suni/agents.py
        dry_run: bool = False,   # show what it WOULD do; execute nothing
    ) -> str:
        """Process a user message through the full agent-tool loop."""
        # Set per-request ContextVars so tool handlers can read them.
        # Both are reset in the finally block — covers early returns and exceptions.
        from ..tools.claude_code_advanced import CLAUDE_API_KEY_CTX
        from ..tools import memory_tool as _memory_tool
        _key_token = CLAUDE_API_KEY_CTX.set(claude_api_key)
        _uid_token = USER_ID_CTX.set(user_id)
        # Role and resolved grants for this turn, so a nested invoke_agent can
        # intersect against what is actually in force rather than re-deriving
        # from the role and discarding the calling agent's narrowing.
        from ..tools.agent_tool import CURRENT_ROLE as _ROLE_CTX
        _role_token = _ROLE_CTX.set(user_role)
        # Bind THIS user's memory manager for the memory_* tools (task-local, so
        # concurrent users never cross). Identity comes from here, not tool args.
        _mem_token = _memory_tool.bind(self.memory)
        try:
            return await self._run_inner(
                user_input, context, user_role, conv_mode, response_language,
                user_mcp_servers, event_cb, user_id, claude_api_key, images,
                agent_profile, dry_run,
            )
        finally:
            CLAUDE_API_KEY_CTX.reset(_key_token)
            USER_ID_CTX.reset(_uid_token)
            _ROLE_CTX.reset(_role_token)
            _memory_tool.reset(_mem_token)

    async def _run_inner(
        self,
        user_input: str,
        context: "Context",
        user_role: str,
        conv_mode: str,
        response_language: str,
        user_mcp_servers,
        event_cb,
        user_id: str,
        claude_api_key: str,
        images: list[str] | None = None,
        agent_profile: dict | None = None,
        dry_run: bool = False,   # show what it WOULD do; execute nothing
    ) -> str:
        # Per-request memory: the task-local binding (set race-free by run() at
        # request start) — NOT self.memory, which _safe_run swaps and a concurrent
        # request can overwrite mid-call. Using the task-local value keeps
        # simultaneous per-channel/per-user conversations from crossing memory.
        from ..tools import memory_tool as _memory_tool
        _mem = _memory_tool.current() or self.memory

        ctx_tokens = estimate_tokens(context.history)
        _log.info("[REQUEST] %r  ctx~%d tok  role=%s mode=%s",
                  user_input[:120], ctx_tokens, user_role, conv_mode)

        # Task mode: handle approve/cancel responses to a pending plan
        if conv_mode == "task" and id(context) in self._pending_plans:
            lower = user_input.strip().lower()
            if any(w in lower for w in ("approve", "yes", "ok", "proceed", "go", "sim", "sim.")):
                saved_calls = self._pending_plans.pop(id(context))
                _log.info("[TASK] plan approved — executing %d tool call(s)", len(saved_calls))
                tool_results = await self._execute_tool_calls(saved_calls)
                for tc, result in tool_results:
                    context.add(Message(role=Role.TOOL, content=str(result),
                                        tool_call_id=tc.id, tool_name=tc.name, agent="orchestrator"))
                # Let model generate the final response from tool results
                final = await self.primary.chat(context.get_conversation(), context, tools=None)
                context.add(final)
                if _mem:
                    await _mem.add_exchange(user_input, final.content)
                return final.content
            else:
                self._pending_plans.pop(id(context), None)
                cancelled = _say("plan_cancelled", response_language)
                context.add(Message(role=Role.ASSISTANT, content=cancelled, agent=self.primary.name))
                if _mem:
                    await _mem.add_exchange(user_input, cancelled)
                return cancelled
        t0 = time.perf_counter()
        trace: list[tuple[str, float, str]] = []  # (label, seconds, note)

        def _tick(label: str, t_start: float, note: str = "") -> float:
            elapsed = time.perf_counter() - t_start
            trace.append((label, elapsed, note))
            return time.perf_counter()

        # ── language override ─────────────────────────────────────────
        lang_hint = _lang_instruction(response_language)
        if lang_hint:
            context.add(Message(role=Role.SYSTEM, content=lang_hint, agent="lang"))

        # Agent profile prompt. Added as a SYSTEM message rather than replacing
        # the base prompt: the base carries the AI-disclosure instruction and the
        # safety rules, and a profile must not be able to drop those.
        if agent_profile and (agent_profile.get("system_prompt") or "").strip():
            context.add(Message(role=Role.SYSTEM,
                                content=agent_profile["system_prompt"].strip(),
                                agent="agent-profile"))

        # ── skills: on-demand, not injected every turn ─────────────────
        # The catalogue used to be injected into every request. Measured on
        # eight two-tool tasks (qwen2.5:7b, temp 0), fully-completed count:
        #
        #   tools only ................ 5/8
        #   prompt WITHOUT catalogue .. 4/8
        #   prompt WITH catalogue ..... 1/8
        #
        # Injecting it cost three of eight completed multi-step tasks. It is not
        # the token size — an equal-sized block of neutral filler scored 6/8 —
        # and not the instructions about skills, which score 2/8 when removed
        # while the catalogue stays. It is the menu itself: given a list of
        # ready-made recipes, the model picks one and stops instead of chaining
        # the two tools the task needs.
        #
        # `skills_list` is a registered tool, so the model can ask for the
        # catalogue when a task actually looks like a stored procedure. That
        # trades some discoverability for the multi-step completion above; the
        # trade is deliberate and measurable with scratchpad/confirm.py.
        if self.skill_store and bool(_cfg.get("skills_inject_catalogue", False)):
            skills_ctx = self.skill_store.level0_context()
            if skills_ctx:
                context.add(Message(role=Role.SYSTEM, content=skills_ctx, agent="skills"))

        # ── memory retrieval (user + collective) ──────────────────────
        ts = time.perf_counter()
        if _mem:
            # ACL-scope collective memory by the caller's role clearance
            _clearance = _rbac.clearance_for_role(user_role)
            mem_context = await _mem.build_context(
                user_input, user_id=user_id, clearance=_clearance
            )
            if mem_context:
                # Replace previous memory injection — Context.add never trims SYSTEM
                # messages, so they accumulate across turns and the DOC-EXCERPT repeats.
                context.history = [m for m in context.history if m.agent != "memory"]
                context.add(Message(role=Role.SYSTEM, content=mem_context, agent="memory"))
        ts = _tick("memory retrieval", ts)

        # Agent profile: resolve its grants against THIS caller's role. The
        # profile is a file a user can edit, so nothing in it is trusted —
        # effective_grants() intersects, never unions. None keeps the plain
        # role behaviour and costs nothing.
        #
        # Resolved HERE, before the collaborate branch below, because a panel
        # agent carries mode=collaborate in its profile. Resolving afterwards
        # meant the branch had already been skipped and the profile's mode
        # reached nothing — the defect this codebase keeps producing, in the
        # code that exists to stop a profile reaching too far.
        _agent_grants = None
        if agent_profile:
            from ..agents import effective_grants as _eff
            from ..tools.agent_tool import CURRENT_GRANTS as _GR
            _agent_grants = _eff(
                agent_profile, user_role,
                getattr(self.registry, "_mcp_prefixes", []),
            )
            if _agent_grants["mode"] != conv_mode and _agent_grants["mode"]:
                conv_mode = _agent_grants["mode"]
            # Traceability: record what this agent was permitted to reach on
            # THIS invocation. Grants are re-derived each time from an
            # editable file and a role that can change, so the profile as it
            # stands later is not evidence of what applied here.
            from ..agents import record_invocation as _rec, mark_used as _used
            _slug = str(agent_profile.get("slug") or "")
            if _slug:
                _rec(_slug, user_id, str(agent_profile.get("_username") or ""),
                     _agent_grants)
                _used(_slug)
            # A profile can name a tool that is not registered — the
            # intersection keeps the name, the registry returns nothing for
            # it, and the model narrates instead of calling. Say so.
            try:
                from ..agents import unknown_tools as _unk
                _missing = _unk(agent_profile, self.registry.names())
                if _missing:
                    _log.warning("[AGENT] %r declares unregistered tool(s): %s — "
                                 "they will not be available",
                                 agent_profile.get("slug", "?"), ", ".join(_missing))
                    if event_cb:
                        event_cb({"type": "agent_warning",
                                  "agent": agent_profile.get("slug", ""),
                                  "unknown_tools": _missing})
            except Exception:      # noqa: BLE001 — a warning is never worth the turn
                pass
            # Daily allowance. Checked here, before any model or tool work, so an
            # agent that has spent its budget costs nothing to refuse. The global
            # tool-iteration cap bounds ONE turn; this bounds a day of them,
            # which is what an unattended schedule actually needs.
            try:
                from ..agents import over_daily_budget as _over
                if _over(agent_profile):
                    _cap = int(agent_profile.get("max_runs_day") or 0)
                    _log.warning("[AGENT] %r has used its daily allowance (%d runs)",
                                 agent_profile.get("slug", "?"), _cap)
                    return (f"The agent {agent_profile.get('name', '?')!r} has used its "
                            f"allowance for today ({_cap} runs). It will run again "
                            f"tomorrow, or raise the limit in the admin panel.")
            except Exception:      # noqa: BLE001 — never fail a turn on a budget check
                pass
            _GR.set(_agent_grants)

        # ── Mode 2: multi-model collaboration (explicit opt-in) ───────────
        # A separate, self-contained branch — the normal Mode-1 pipeline below is
        # left entirely untouched. Runs only when the user selects "collaborate"
        # mode AND the master switch is on.
        if conv_mode == "collaborate" and bool(_cfg.get("collaborate_enabled", True)):
            from . import orchestrate as _collab
            _hist = [m for m in context.history
                     if m.role in (Role.USER, Role.ASSISTANT) and m.content][-4:]
            _ctx_hint = "\n".join(
                f"{'User' if m.role == Role.USER else 'SUNI'}: {m.content[:300]}" for m in _hist)
            # A panel agent: its profile carries mode=collaborate, so it lands
            # here instead of a single-model turn. The pool may be per-agent —
            # a reviewer's panel is not a summariser's — and the agent's own
            # instructions shape only the SYNTHESIS. Applying a persona to every
            # seat would correlate the models, and decorrelation is the whole
            # reason a panel earns its cost.
            _pool = (agent_profile or {}).get("pool") or None
            _persona = (agent_profile or {}).get("system_prompt", "") if agent_profile else ""
            final = await _collab.run_collaboration(
                user_input, event_cb=event_cb,
                lang_hint=_lang_instruction(response_language), context_hint=_ctx_hint,
                pool=_pool, persona=_persona)
            context.add(Message(role=Role.USER, content=user_input))
            _who = (agent_profile or {}).get("name") if agent_profile else ""
            if _who:
                final = f"[{_who} · panel]" + _NL + str(final)
            context.add(Message(role=Role.ASSISTANT, content=final, agent="collaborate"))
            if _mem:
                await _mem.add_exchange(user_input, final)
            _tick("collaborate", ts)
            return final

        # ── web prefetch ──────────────────────────────────────────────
        triggered = _WEB_RE.search(user_input) is not None
        ts = time.perf_counter()
        await self._maybe_prefetch(user_input, context)
        _tick("web prefetch", ts, "triggered" if triggered else "skipped")

        # ── task routing hint ─────────────────────────────────────────
        route = self._router.route(user_input)
        _log.info("[ROUTE]   %s", route)
        from ..user_settings import resolve_output_dir as _rod
        _out_dir = _rod(user_id)
        hint = self._router.get_hint(route, output_dir=_out_dir)
        # Name the agents that actually exist. The hint alone was not enough: a
        # 7B tier told to "call invoke_agent with the name they used" still
        # reached for run_shell, because it had evidence run_shell exists and
        # none that the agent does. Listing them turns a guess into a lookup.
        if route in ("agent", "schedule") and user_id:
            try:
                from ..agents import list_for_user as _lfu
                _av = [a for a in _lfu(user_id, user_role) if a.get("enabled", True)]
                if _av:
                    _names = "; ".join(
                        f"{a['name']} (slug: {a['slug']})"
                        + (f" — {a['description']}" if a.get("description") else "")
                        for a in _av)
                    hint = (hint or "") + _NL + f"[Agents available to this user: {_names}]"
                elif route == "agent":
                    hint = (hint or "") + _NL + (
                        "[This user has NO agents defined. Say so plainly instead of "
                        "doing the work and presenting it as an agent's.]")
            except Exception:      # noqa: BLE001 — a hint is never worth a failed turn
                pass
        if hint:
            context.add(Message(role=Role.SYSTEM, content=hint, agent="router"))
            console.print(f"  [dim]→ router: [bold]{route}[/bold][/dim]")

        user_msg = Message(role=Role.USER, content=user_input)
        context.add(user_msg)

        # ── context compression ───────────────────────────────────────
        ts = time.perf_counter()
        compressed = await self._compressor.compress(context, self.primary, trace)
        if compressed:
            _tick("ctx compress", ts)

        # ── read-only mode: block all write/send direct paths ────────
        _readonly = (conv_mode == "read-only")

        # ── upfront Claude Code routing ───────────────────────────────
        # Gate: not read-only, not task-mode (needs plan/approve), role allows
        # claude_task, T5 is registered, and classifier says it's code/system work.
        _cc_allowed = _rbac.allowed_tools(user_role)
        _cc_rbac_ok = (
            "claude_task" not in _rbac.blocked_tools(user_role)
            and (_cc_allowed is None or "claude_task" in _cc_allowed)
        )
        _force_cc = bool(_cfg.get("force_claude_code"))
        _cc_should_direct = (
            not _readonly
            and conv_mode != "task"
            and _cc_rbac_ok
            and CLAUDE_CODE_TIER in self._tier_agents
            and (_force_cc or direct_to_claude_code(user_input))
        )

        # Was the previous turn an image we generated? If so, a short follow-up
        # ("referia-me a…", "make it bigger", "actually a cat") is a refinement of
        # that image — regenerate — not a brand-new query for the agent to route.
        _last_asst = next(
            (m for m in reversed(context.history) if m.role == Role.ASSISTANT), None
        )
        # Detect from CONTENT (the persisted serve-URL), not just the transient
        # agent="image" attribute — the web path rebuilds Context from the DB
        # without that attribute, so an attribute-only check would silently
        # no-op refinements after a restart or when resuming an old conversation.
        _last_was_image = bool(_last_asst and (
            getattr(_last_asst, "agent", "") == "image"
            or re.search(r'/api/files/serve\?path=[^\s]+\.(?:png|jpe?g|webp)',
                         _last_asst.content, re.IGNORECASE)
        ))

        # ── direct PDF path ───────────────────────────────────────────
        # Skip direct path when request includes a URL or asks to fetch/look at a page —
        # those need the agent loop to web_fetch content first.
        _pdf_needs_fetch = bool(re.search(
            r'https?://|'
            r'\b(look\s+at|take\s+a\s+look|fetch|read\s+the\s+(page|site)|'
            r'from\s+this\s+(page|site|url)|based\s+on\s+this\s+(page|url))\b',
            user_input, re.IGNORECASE,
        ))
        # ── direct vision path (image attachment + VLM configured) ────
        if images and _vision.enabled():
            ts = time.perf_counter()
            response = await self._handle_vision_direct(user_input, images, trace)
            context.add(response)
            _tick("direct vision", ts)
        # The direct-pdf path is an optimisation for "put THIS in a PDF": it
        # takes the last substantial assistant message as the document body. If
        # there is nothing to compile — "create a PDF about Coimbra" as a first
        # turn — its precondition does not hold, and it used to answer with a
        # hardcoded English refusal asking the user to compile the text first.
        # A fast path whose precondition fails must hand back to the general
        # path, where the model can research the topic and call the tools, not
        # dead-end. (It also bypassed the response-language instruction, so a
        # pt-PT user was refused in English.)
        elif route == "pdf" and not _readonly and not _pdf_needs_fetch \
                and "create_pdf" in self.registry.names() \
                and _compiled_content(context):
            ts = time.perf_counter()
            response = await self._handle_pdf_direct(user_input, context, trace)
            context.add(response)
            _tick("direct pdf", ts)
        # ── direct email path ─────────────────────────────────────────
        elif route == "agent" and not _readonly:
            _direct = await self._handle_agent_direct(
                user_input, trace, user_id, user_role, event_cb=event_cb)
            if _direct is not None:
                response = Message(role=Role.ASSISTANT, content=_direct, agent="delegate")
                context.add(response)
            else:
                response = await self._agent_loop(context, trace, route,
                                                  user_role=user_role, conv_mode=conv_mode,
                                                  user_mcp_servers=user_mcp_servers,
                                                  event_cb=event_cb,
                                                  starting_tier=_start_tier,
                                                  user_id=user_id,
                                                  user_input=user_input,
                                                  cc_rbac_ok=_cc_rbac_ok,
                                                  grants=_agent_grants)
                context.add(response)
        elif route == "schedule" and not _readonly:
            # Deterministic first: parsing beats tool-selection here, and it
            # asks for anything missing instead of inventing it. Returns None
            # when the text only looked recurring, and the normal pipeline runs.
            _direct = await self._handle_schedule_direct(
                user_input, trace, user_id, user_role, event_cb=event_cb)
            if _direct is not None:
                # Every branch here must yield a Message: _run_inner ends with
                # response.content. Returning the bare string produced an empty
                # reply — the handler ran in 0.9s, answered correctly, and the
                # user saw nothing.
                response = Message(role=Role.ASSISTANT, content=_direct, agent="schedule")
                context.add(response)
            else:
                response = await self._agent_loop(context, trace, route,
                                                  user_role=user_role, conv_mode=conv_mode,
                                                  user_mcp_servers=user_mcp_servers,
                                                  event_cb=event_cb,
                                                  starting_tier=_start_tier,
                                                  user_id=user_id,
                                                  user_input=user_input,
                                                  cc_rbac_ok=_cc_rbac_ok,
                                                  grants=_agent_grants)
                context.add(response)
        elif route == "email" and not _readonly and "send_email" in self.registry.names():
            ts = time.perf_counter()
            response = await self._handle_email_direct(user_input, context, trace)
            context.add(response)
            _tick("direct email", ts)
        # ── direct image-generation path (local Stable Diffusion) ─────
        # Runs regardless of force_claude_code, since generate_image is a SUNI
        # tool the Claude Code CLI agent doesn't have.
        elif (not _readonly and bool(_cfg.get("image_gen_enabled", True))
              and "generate_image" in self.registry.names()
              and (_IMAGE_GEN_RE.search(user_input)
                   or (_last_was_image and _IMAGE_REFINE_RE.search(user_input)))):
            ts = time.perf_counter()
            response = await self._handle_image_direct(
                user_input, context, trace,
                is_refinement=(_last_was_image and not _IMAGE_GEN_RE.search(user_input)),
            )
            context.add(response)
            _tick("direct image", ts)
        # ── direct Claude Code path ───────────────────────────────────
        elif _cc_should_direct:
            ts = time.perf_counter()
            response = await self._handle_claude_code_direct(
                user_input, context, trace, event_cb=event_cb
            )
            context.add(response)
            _tick("claude-code direct", ts)
        else:
            # ── agent + tool loop ─────────────────────────────────────
            # Determine starting tier: complexity score, floored at the CORE tier
            # (qwen2.5:7b) and capped at the hardware limit. Sub-core models (the
            # 1.5B nano) are too weak for reliable tool use — they hallucinate
            # instead of calling tools — so we never start below core; harder
            # queries still climb from there and escalate to Claude Code (T5).
            _start_tier = min(max(complexity_score(user_input), DEFAULT_TIER), MAX_LOCAL_TIER)
            # Delegation and scheduling are structured tool-selection tasks, and
            # the core tier is measurably bad at them: with the right tool
            # registered, the hint injected and the agent named, observed runs
            # still reached for run_shell and db_query instead. Start these a
            # tier higher — the model has to pick one tool out of ~57 and get its
            # arguments right, which is exactly what the smaller models fail at.
            if route in ("agent", "schedule"):
                _start_tier = min(max(_start_tier + 1, DEFAULT_TIER + 1), MAX_LOCAL_TIER)
                # Automatic escalation when nothing local is up to it. These are
                # structured tool-selection tasks, and the observed failure was
                # not a near miss — four runs of one prompt picked four
                # different wrong tools. Below tier 3 the honest thing is to
                # hand off rather than produce a confident wrong answer, so if
                # T5 is registered and permitted, go straight there.
                if MAX_LOCAL_TIER < _MIN_TIER_FOR_STRUCTURED and _cc_rbac_ok                         and CLAUDE_CODE_TIER in self._tier_agents:
                    _log.info("[TIER] no local model above tier %d for a %s task — "
                              "escalating to T5", MAX_LOCAL_TIER, route)
                    if event_cb:
                        event_cb({"type": "tier_escalate", "from": MAX_LOCAL_TIER,
                                  "to": CLAUDE_CODE_TIER, "reason": "no_capable_local"})
                    _start_tier = CLAUDE_CODE_TIER
            # An agent that declares tools needs a model that can actually call
            # them. Measured: a delegated agent answered with the literal text
            # ping_host("localhost") instead of invoking it — the profile was
            # correct, the tool was permitted, and the model wrote a function
            # call as prose. Declaring tools is the user saying "this agent uses
            # these", so treat it as a capability requirement rather than making
            # them know which local model does function-calling well.
            #
            # An explicitly pinned model is left alone: that is a deliberate
            # choice, and silently overriding it would be the same class of bug
            # as escalation swapping a pinned model out mid-run.
            if agent_profile and (agent_profile.get("tools") is not None)                     and not (_agent_grants or {}).get("model"):
                if MAX_LOCAL_TIER >= _MIN_TIER_FOR_TOOL_USE:
                    if _start_tier < _MIN_TIER_FOR_TOOL_USE:
                        _log.info("[TIER] agent %r declares tools — raising %d→%d",
                                  agent_profile.get("slug", "?"), _start_tier,
                                  _MIN_TIER_FOR_TOOL_USE)
                        _start_tier = _MIN_TIER_FOR_TOOL_USE
                elif _cc_rbac_ok and CLAUDE_CODE_TIER in self._tier_agents:
                    _log.info("[TIER] agent %r declares tools and no local model "
                              "reaches tier %d — escalating to T5",
                              agent_profile.get("slug", "?"), _MIN_TIER_FOR_TOOL_USE)
                    if event_cb:
                        event_cb({"type": "tier_escalate", "from": MAX_LOCAL_TIER,
                                  "to": CLAUDE_CODE_TIER, "reason": "agent_needs_tools"})
                    _start_tier = CLAUDE_CODE_TIER
                else:
                    # Nothing capable and no handoff available. Say so rather
                    # than let it fail quietly as prose-instead-of-tool-call.
                    _log.warning("[TIER] agent %r declares tools but the best local "
                                 "tier is %d and Claude Code is unavailable — tool "
                                 "calls may not be reliable",
                                 agent_profile.get("slug", "?"), MAX_LOCAL_TIER)

            _log.info("[TIER]    start=%d  max_local=%d (floor=core)", _start_tier, MAX_LOCAL_TIER)
            ts = time.perf_counter()
            response = await self._agent_loop(context, trace, route,
                                              user_role=user_role, conv_mode=conv_mode,
                                              user_mcp_servers=user_mcp_servers,
                                              event_cb=event_cb,
                                              starting_tier=_start_tier,
                                              user_id=user_id,
                                              user_input=user_input,
                                              cc_rbac_ok=_cc_rbac_ok,
                                              grants=_agent_grants, dry_run=dry_run)
            context.add(response)

        # ── memory store ──────────────────────────────────────────────
        ts = time.perf_counter()
        if _mem:
            await _mem.add_exchange(user_input, response.content)
        _tick("memory store", ts)

        # ── auto-config detection ──────────────────────────────────────
        if user_id:
            try:
                from .. import auto_config as _ac
                changes = _ac.detect_and_apply(user_id, user_input, response.content)
                if changes and event_cb:
                    event_cb({"type": "auto_config", "changes": changes})
            except Exception:
                pass

        # ── skill auto-generation ──────────────────────────────────────
        # Count distinct tool calls in this turn's trace
        tool_calls_count = sum(
            1 for lbl, _, _ in trace if lbl.startswith("tool(s):")
        )
        if self.skill_store and tool_calls_count >= 5:
            asyncio.create_task(
                self._auto_generate_skill(user_input, response.content, trace)
            )

        total = time.perf_counter() - t0
        self._print_trace(trace, total, user_input)

        # Structured log entry
        tools_flat = [t for lbl, _, _ in trace if lbl.startswith("tool(s):") for t in lbl[8:].split(", ")]
        infer_note = next((n for lbl, _, n in trace if "model inference" in lbl), "")
        _log.info("[DONE]    total=%.2fs route=%s tools=[%s] %s",
                  total, route, ", ".join(tools_flat), infer_note)

        # Record in activity log for dashboard
        tools_used = [
            lbl.replace("tool(s): ", "")
            for lbl, _, _ in trace if lbl.startswith("tool(s):")
        ]
        infer = next(
            ((e, n) for lbl, e, n in trace if "model inference" in lbl), (0, "")
        )
        activity_log.append({
            "ts":        datetime.now(timezone.utc).isoformat(),
            "query":     user_input[:80],
            "route":     route,
            "tools":     tools_used,
            "total_s":   round(total, 2),
            "infer_s":   round(infer[0], 2),
            "trace_note": infer[1],
        })

        return response.content

    async def _handle_claude_code_direct(
        self, user_input: str, context: Context, trace: list, event_cb=None
    ) -> Message:
        """
        Route directly to the ClaudeCodeAgent (T5) for self-contained
        code/system tasks identified upfront by the classifier.

        Skips the local-model tier loop and Ollama tool list entirely.
        RBAC and conv_mode guards are applied by the caller in run().
        """
        if event_cb:
            event_cb({"type": "tier_route", "tier": CLAUDE_CODE_TIER, "reason": "classifier"})

        agent = self._tier_agents[CLAUDE_CODE_TIER]
        ts = time.perf_counter()
        response = await agent.chat(context.get_conversation(), context)
        elapsed = time.perf_counter() - ts
        trace.append(("claude-code direct", elapsed, f"t{CLAUDE_CODE_TIER}"))
        _log.info("[CC_DIRECT] %.2fs  agent=%s", elapsed, agent.name)
        return response

    async def _handle_vision_direct(
        self, user_input: str, images: list[str], trace: list
    ) -> Message:
        """
        Single-turn image understanding: send the attached image(s) + the user's
        prompt to the configured VLM and return its text answer. Images bypass the
        Message/context/memory machinery entirely (see vision.py); the answer
        flows back normally, so follow-up turns work off the description.
        """
        ts = time.perf_counter()
        prompt = user_input or "Describe this image."
        try:
            text = await _vision.describe(images, prompt)
        except _vision.VisionError as exc:
            _log.warning("[VISION] %s", exc)
            text = f"I couldn't analyze the image: {exc}"
        except Exception as exc:
            _log.error("[VISION] unexpected: %s", exc, exc_info=True)
            text = _say("image_failed")
        trace.append((f"vision ({len(images)} image(s))", time.perf_counter() - ts, ""))
        return Message(role=Role.ASSISTANT, content=_sanitize_response(text),
                       agent=self.primary.name)

    async def _handle_image_direct(
        self, user_input: str, context: Context, trace: list,
        is_refinement: bool = False,
    ) -> Message:
        """Generate an image locally (Stable Diffusion) and return it — the reply
        carries an /api/files/serve URL so the Face stage displays it.

        When ``is_refinement`` is set, the previous turn was an image and this
        request is a follow-up ("referia-me a…", "make it bigger"): strip the
        clarification cue and, if what remains is just a modifier, fold it into
        the previous prompt rather than starting from an empty subject."""
        import re as _re
        from ..tools import image_tool as _img
        ts = time.perf_counter()

        # ── size: explicit WxH / "Npx" / keywords (English + Portuguese) ──────
        _t = user_input.lower()
        w = h = 512
        _mwh = _re.search(r'\b(\d{3,4})\s*(?:x|×|by|por)\s*(\d{3,4})\b', _t)
        _mpx = _re.search(r'\b(\d{3,4})\s*(?:px|pixe(?:ls|l|is))\b', _t)
        if _mwh:
            w, h = int(_mwh.group(1)), int(_mwh.group(2))
        elif _mpx:
            w = h = int(_mpx.group(1))
        elif _re.search(r'\b(portrait|retrato|vertical)\b', _t):        w, h = 512, 768
        elif _re.search(r'\b(landscape|paisagem|horizontal|wide|panor[âa]mic)', _t): w, h = 768, 512
        elif _re.search(r'\b(large|big|grande|hi-?res|high[ -]?res)', _t): w, h = 768, 768
        elif _re.search(r'\b(small|tiny|pequen[oa]|thumbnail)', _t):     w, h = 384, 384
        _clamp = lambda v: max(256, min(1024, (int(v) // 8) * 8))   # SD needs multiples of 8
        w, h = _clamp(w), _clamp(h)

        # ── prompt: drop a leading "generate/create … image of/showing …" phrase
        # (English + Portuguese/Spanish connectors), then strip any size tokens.
        m = _re.search(
            r'(?:image|picture|illustration|drawing|artwork|art|photo|poster|logo|graphic|'
            r'painting|wallpaper|avatar|icon|imagem|foto|il+ustra[çc][ãa]o|desenho|imagen|dibujo|retrato)\s+'
            r'(?:of|showing|with|depicting|de|do|da|com|mostrando|:)\s+(.+)',
            user_input, _re.IGNORECASE)
        prompt = (m.group(1).strip() if m else user_input.strip())
        prompt = _re.sub(r'\b\d{3,4}\s*(?:x|×|by|por)\s*\d{3,4}\b', '', prompt)
        prompt = _re.sub(r'\b\d{3,4}\s*(?:px|pixe(?:ls|l|is))\b', '', prompt).strip(" ?!.,")
        if not prompt:
            prompt = user_input.strip()

        # ── refinement of the previous image ─────────────────────────────────
        # Strip the leading clarification cue ("referia-me ao…", "actually…",
        # "quero dizer…"); fold pure modifiers ("maior", "more realistic") into
        # the prior prompt so we keep the original subject.
        if is_refinement:
            prompt = _re.sub(
                r'^\s*(referia-?me\s+(a[o]?\s+)?|na verdade[,: ]*|antes[,: ]*|'
                r'quero dizer[,: ]*|queria (dizer )?[,: ]*|quis dizer[,: ]*|'
                r'i meant[,: ]*|actually[,: ]*|no[,: ]+|rather[,: ]*|instead[,: ]*|'
                r'not that[,: ]*|make it[,: ]*|but[,: ]+)',
                '', prompt, flags=_re.IGNORECASE,
            ).strip(" ?!.,")
            _prev = (context.get("last_image_prompt") or "").strip()
            # A short leftover (≤2 words) reads as a modifier, not a new subject.
            if _prev and (not prompt or len(prompt.split()) <= 2):
                prompt = f"{_prev}, {prompt}".strip(" ,") if prompt else _prev
            if not prompt:
                prompt = _prev or user_input.strip()

        context.set("last_image_prompt", prompt)
        result = await _img.handler(prompt=prompt, width=w, height=h)
        trace.append(("image gen", time.perf_counter() - ts, f"{w}x{h}"))
        return Message(role=Role.ASSISTANT, content=result, agent="image")

    async def _handle_pdf_direct(
        self, user_input: str, context: Context, trace: list
    ) -> Message:
        """
        Create a PDF by delegating to Claude Code (claude_task).
        Claude Code writes proper Python to generate a well-formatted PDF,
        handling Unicode, tables, and layout correctly.
        Falls back to the native create_pdf tool if claude_task is unavailable.
        """
        import re as _re
        from pathlib import Path as _Path

        # Same helper the dispatch guard uses, so the two cannot disagree about
        # whether this path applies.
        content = _compiled_content(context)

        if not content:
            return Message(
                role=Role.ASSISTANT,
                content=_say("no_compiled_content"),
                agent=self.primary.name,
            )

        # Derive filename
        words = _re.sub(r'[^\w\s]', '', user_input).split()
        skip = {'put','this','compilation','in','a','pdf','on','my','desktop',
                'make','create','generate','save','the','to','into','please','file'}
        topic_words = [w for w in words if w.lower() not in skip]
        slug = "_".join(topic_words[:4]) if topic_words else "document"
        filename = f"{slug}.pdf"
        from ..user_settings import resolve_output_dir as _rod
        _out_dir = _rod(USER_ID_CTX.get(""))
        path = str(_Path(_out_dir) / filename)

        ts = time.perf_counter()

        _log.info("[PDF_DIRECT] using create_pdf → %s (%d chars)", path, len(content))
        result = await self.registry.execute("create_pdf", {
            "content": content, "path": path, "title": slug.replace("_", " ")
        })

        elapsed = time.perf_counter() - ts
        trace.append(("pdf generation", elapsed, str(result)[:80]))
        _log.info("[PDF_DONE] %.2fs → %s", elapsed, str(result)[:120])

        reply = f"PDF saved as `{filename}`.\n\n{result}"
        return Message(role=Role.ASSISTANT, content=reply, agent=self.primary.name)



    async def _handle_agent_direct(
        self, user_input: str, trace: list, user_id: str, user_role: str,
        event_cb=None,
    ) -> str | None:
        """Delegate to a named agent without asking a model to choose a tool.

        Returns the agent's answer, or None to fall through.

        Same reasoning as the scheduling path: the model has to pick invoke_agent
        out of ~57 tools and fill in the name, and measurement said it does not.
        Extracting the name is a regex problem. Deciding what to do once the
        agent is identified is not, so the sub-turn is still a full model turn —
        run under the agent's profile with grants intersected as usual.
        """
        from . import schedule_intent as _si
        from .. import agents as _agents
        from ..tools import agent_tool as _at

        try:
            known = [a for a in _agents.list_for_user(user_id, user_role)
                     if a.get("enabled", True)]
        except Exception:
            known = []
        parsed = _si.parse(user_input, known)
        if not parsed["agent_named"]:
            return None                     # no agent named — ordinary request

        if not parsed["agent_slug"]:
            # Refuse by name rather than quietly doing it myself and presenting
            # the result as the agent's. Being wrong loudly is recoverable.
            avail = ", ".join(a["name"] for a in known) if known else "none defined"
            return (f"I have no agent called {parsed['agent_named']!r}. "
                    f"Available: {avail}. Say which to use, or ask me to do it myself.")

        profile = _agents.get(parsed["agent_slug"])
        if not profile:
            return f"The agent {parsed['agent_named']!r} could not be loaded."
        profile["_username"] = ""

        task = _si.strip_delegation(user_input)
        ts = time.perf_counter()
        depth_token = _at.AGENT_DEPTH.set(_at.AGENT_DEPTH.get(0) + 1)
        try:
            sub = Context()
            answer = await self._safe_run(
                task, sub, user_role=user_role, user_id=user_id,
                agent_profile=profile, event_cb=event_cb,
            )
        except Exception as exc:      # noqa: BLE001 — say it failed, do not answer instead
            return f"The agent {profile['name']!r} failed: {exc}"
        finally:
            _at.AGENT_DEPTH.reset(depth_token)
        trace.append((f"delegated to {profile['name']}", time.perf_counter() - ts, ""))
        return f"[{profile['name']}]" + _NL + str(answer)

    async def _handle_schedule_direct(
        self, user_input: str, trace: list, user_id: str, user_role: str,
        event_cb=None,
    ) -> str | None:
        """Set up a recurring run without asking a model to pick a tool.

        Returns the reply, or None to fall through to the normal pipeline.

        The structured part — cadence, delivery, which agent — is parsed by
        regex, because that is the part the local tiers get wrong. Measured, not
        assumed: repeated runs of the same prompt called skills_list, run_shell,
        db_query and send_email, never create_schedule, with the tool registered
        and named in an injected hint.
        """
        from . import schedule_intent as _si
        from .. import agents as _agents
        from .. import schedules as _s

        try:
            known = [a for a in _agents.list_for_user(user_id, user_role)
                     if a.get("enabled", True)]
        except Exception:
            known = []
        parsed = _si.parse(user_input, known)
        if not parsed["recurring"]:
            return None                       # not a scheduling request after all

        # Ask before building. This is the whole reason for the direct path: an
        # unattended job built on a guessed detail is wrong every single run.
        q = _si.question(parsed)
        if q:
            _tick = trace.append
            _tick(("schedule: asked for missing detail", 0.0, ""))
            extra = ""
            if parsed["agent_named"] and not parsed["agent_slug"]:
                extra = (f" Also, I have no agent called {parsed['agent_named']!r} — "
                         f"tell me which of these to use, or I will handle it myself: "
                         + (", ".join(a["name"] for a in known) if known else "none defined")
                         + ".")
            return q + extra

        if parsed["agent_named"] and not parsed["agent_slug"]:
            return (f"I have no agent called {parsed['agent_named']!r}. Available: "
                    + (", ".join(a["name"] for a in known) if known else "none") +
                    ". Say which to use, or ask me to do it myself.")

        # The stored prompt runs later with no conversation history, so strip the
        # scheduling and delivery clauses — otherwise the run would try to email
        # itself a second time.
        task = _si.strip_scheduling(user_input)
        name = _si.suggest_name(task)
        delivery = {"type": "email", "to": parsed["email_to"]} if parsed["email_to"] else {}

        # Same gate the tool would have hit: recurring unattended execution.
        if event_cb:
            decision = await _approval.request_approval(
                "create_schedule",
                {"name": name, "cadence": parsed["cadence"],
                 "email_to": parsed["email_to"] or "(not emailed)",
                 "agent": parsed["agent_slug"] or "(SUNI)", "prompt": task},
                user_id, event_cb,
            )
            if decision != "allow":
                return "Left it unscheduled."

        try:
            rec = _s.create(name=name, prompt=task, cadence=parsed["cadence"],
                            owner_id=user_id, agent_slug=parsed["agent_slug"],
                            delivery=delivery)
        except _s.CadenceError as exc:
            return f"I could not schedule that: {exc}"

        bits = [f"Scheduled: {parsed['cadence']}, first run "
                f"{rec['next_run'][:16].replace('T', ' ')} UTC"]
        if parsed["agent_slug"]:
            bits.append(f"handled by {parsed['agent_slug']}")
        if delivery:
            bits.append(f"emailed to {delivery['to']}")
        return ". ".join(bits) + f". Ask me to list your schedules to change it. (id: {rec['id']})"

    async def _handle_email_direct(
        self, user_input: str, context: Context, trace: list
    ) -> Message:
        """
        Send an email directly, bypassing the LLM tool-call path.
        Extracts recipient from user input, body from last compiled content,
        and attachment from the most recently created PDF in context.
        """
        import re as _re

        # Extract email address(es) from user input
        recipients = _re.findall(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}', user_input)
        if not recipients:
            return Message(
                role=Role.ASSISTANT,
                content=_say("no_email_address", response_language),
                agent=self.primary.name,
            )

        # Find the best body: last substantial ASSISTANT message
        body = ""
        for msg in reversed(context.history):
            if msg.role == Role.ASSISTANT and len(msg.content) > 80:
                body = msg.content
                break

        if not body:
            body = _say("attached", response_language)

        # Find subject: first meaningful line of body or user_input hint
        subject_match = _re.search(r'subject[:\s]+([^\n]+)', user_input, _re.IGNORECASE)
        if subject_match:
            subject = subject_match.group(1).strip()
        else:
            first_line = next((l.strip() for l in body.splitlines() if l.strip()), "")
            # Strip markdown
            subject = _re.sub(r'[*#\[\]]', '', first_line)[:80] or "Information from SUNI"

        # Find PDF attachment — search ASSISTANT messages only (avoids SYSTEM/memory matches)
        attachment = ""
        pdf_pattern = _re.compile(r'[A-Za-z]:[/\\][^\s"\'<>|*?]+\.pdf', _re.IGNORECASE)
        for msg in reversed(context.history):
            if msg.role != Role.ASSISTANT:
                continue
            m = pdf_pattern.search(msg.content)
            if m:
                candidate = m.group(0).replace('/', '\\')
                if _Path(candidate).exists():   # only attach files that actually exist
                    attachment = candidate
                    break

        # Send to each recipient
        results = []
        ts = time.perf_counter()
        for to in recipients:
            args = {"to": to, "subject": subject, "body": body}
            if attachment:
                args["attachment_path"] = attachment
            result = await self.registry.execute("send_email", args)
            results.append(f"{to}: {result}")
            _log.info("[EMAIL_DIRECT] → %s attachment=%s", to, attachment or "none")

        elapsed = time.perf_counter() - ts
        trace.append(("send_email (direct)", elapsed, "; ".join(results)[:80]))

        attach_note = f" with attachment `{attachment.split(chr(92))[-1]}`" if attachment else ""
        reply = "Email sent" + attach_note + ".\n\n" + "\n".join(results)
        return Message(role=Role.ASSISTANT, content=reply, agent=self.primary.name)

    async def _safe_run(
        self,
        user_input: str,
        context: Context,
        memory_override=None,
        user_role: str = "admin",
        conv_mode: str = "assistant",
        response_language: str = "",
        user_mcp_servers: list[str] | None = None,
        event_cb=None,
        user_id: str = "",
        claude_api_key: str = "",
        images: list[str] | None = None,
        agent_profile: dict | None = None,
        dry_run: bool = False,   # show what it WOULD do; execute nothing
    ) -> str:
        """Wrapper that guarantees any unhandled exception is logged."""
        original_memory = self.memory
        if memory_override is not None:
            self.memory = memory_override
        try:
            return await self.run(user_input, context,
                                  user_role=user_role, conv_mode=conv_mode,
                                  response_language=response_language,
                                  user_mcp_servers=user_mcp_servers,
                                  event_cb=event_cb, user_id=user_id,
                                  images=images,
                                  claude_api_key=claude_api_key, agent_profile=agent_profile,
                                  dry_run=dry_run)
        except _bhealth.BackendUnavailableError as exc:
            # Breaker is open — the local model backend is down. Return a clean,
            # user-facing message. run() yields a STRING (_run_inner returns
            # response.content), so this handler must too — returning a Message
            # here would break the caller's str handling (response.split()).
            _log.warning("[BACKEND] fast-fail (breaker open): %s", exc)
            return ("⚠ The local model backend is temporarily unavailable. "
                    "It's being checked automatically — please try again in a moment.")
        except Exception as exc:
            _log.error("[CRASH] unhandled exception in run(): %s", exc, exc_info=True)
            raise
        finally:
            self.memory = original_memory

    async def _agent_loop(
        self,
        context: Context,
        trace: list,
        route: str = "local",
        user_role: str = "admin",
        conv_mode: str = "assistant",
        user_mcp_servers: list[str] | None = None,
        event_cb=None,
        starting_tier: int = 0,     # 0 = use DEFAULT_TIER
        user_id: str = "",
        user_input: str = "",       # original user text; used for T5 fallback
        cc_rbac_ok: bool = True,    # whether T5 is permitted for this user/role
        grants: dict | None = None, # resolved agent grants; None = plain role
        dry_run: bool = False,      # stop before executing anything
    ) -> Message:
        # MCP prefix filtering based on route AND role (AND per-user restriction)
        registered_prefixes = getattr(self.registry, "_mcp_prefixes", [])
        # An agent profile can only ever narrow these: suni.agents.effective_grants()
        # has already intersected them with the role before we get here, so there is
        # no path by which a profile widens what this user could otherwise reach.
        if grants:
            role_prefixes = grants["mcp_prefixes"]
        else:
            role_prefixes = _rbac.mcp_prefixes(user_role, registered_prefixes)

        # Per-user MCP restriction: intersect role prefixes with user's allowed list
        if user_mcp_servers is not None and role_prefixes is not None:
            role_prefixes = [p for p in role_prefixes if p in user_mcp_servers]

        # Keep playwright active if it was used earlier in this conversation
        _browser_active = any(
            msg.role == Role.TOOL
            and msg.tool_name
            and msg.tool_name.startswith("playwright_")
            for msg in context.history[-30:]
        )

        include: list[str] = []
        if conv_mode == "read-only":
            include = []   # no MCP tools in read-only mode
        elif (route == "browser" or _browser_active) and "playwright" in (role_prefixes or []):
            include.append("playwright")
            if "filesystem" in (role_prefixes or []):
                include.append("filesystem")
        else:
            if "filesystem" in (role_prefixes or []):
                include.append("filesystem")

        tools = self.registry.get_ollama_tools(
            include_prefixes=include,
            allowed_tools=(grants["allowed_tools"] if grants else _rbac.allowed_tools(user_role)),
            blocked_tools=(grants["blocked_tools"] if grants else _rbac.blocked_tools(user_role)),
        )

        # Tier setup: pick starting agent, allow escalation up through all local tiers then T5
        current_tier  = starting_tier if starting_tier > 0 else DEFAULT_TIER
        current_agent = self._agent_for_tier(current_tier) or self.primary

        # An agent profile may pin a model. Escalation is then suppressed for the
        # rest of this request: the point of choosing a model is that it answers,
        # and a tier step would silently swap it out mid-run — the setting would
        # appear to work while doing nothing on exactly the harder prompts it was
        # chosen for. Falling back to the tier system on construction failure is
        # deliberate; refusing to answer would be worse than answering by default.
        _pinned = False
        _pin_model = (grants or {}).get("model") or ""
        if _pin_model:
            pinned_agent = self._agent_for_model(_pin_model)
            if pinned_agent is not None:
                current_agent = pinned_agent
                _pinned = True
                _log.info("[AGENT] model pinned to %s by agent profile", _pin_model)
            else:
                _log.warning("[AGENT] could not build agent for pinned model %r — "
                             "falling back to the tier system", _pin_model)
        _t5_available = cc_rbac_ok and CLAUDE_CODE_TIER in self._tier_agents

        # Emit initial tier so UI can show the orb in the right state immediately
        if event_cb:
            event_cb({"type": "tier_route", "tier": current_tier, "reason": "start"})

        # Cap on tool-call rounds. Configurable via the admin panel
        # (max_tool_iterations); falls back to the module default if unset/invalid.
        _max_iters = int(_cfg.get("max_tool_iterations", MAX_TOOL_ITERATIONS) or MAX_TOOL_ITERATIONS)
        # An agent may carry a tighter step budget than the global cap. Only
        # tighter: taking the min means a profile cannot buy itself more room
        # than the instance allows, which is the same rule as its tool grants.
        _ag_steps = int((grants or {}).get("max_steps") or 0)
        if _ag_steps > 0:
            _max_iters = min(_max_iters, _ag_steps)

        # Behavioural escalation signal — a confidently-wrong model produces failing
        # tool calls, which the refusal-phrase heuristic never catches. Track tool
        # errors and unproductive re-planning so we can escalate on *behaviour*.
        _tool_fail_counts: dict[str, int] = {}   # tool name -> cumulative errors this task
        _rounds_without_success = 0              # consecutive tool rounds with no success

        for iteration in range(_max_iters):
            ts = time.perf_counter()
            response = await current_agent.chat(
                context.get_conversation(), context, tools=tools
            )
            elapsed = time.perf_counter() - ts
            note = getattr(response, "_trace_note", "")
            tier_label = f"t{current_tier}" if current_tier else ""
            trace.append((f"model inference #{iteration + 1} [{tier_label}]", elapsed, note))

            # Escalation: step up tiers on capability failure; final stop is T5
            if (not _pinned and not response.has_tool_calls()
                    and needs_escalation(response.content)):
                next_tier = current_tier + 1
                next_agent = self._tier_agents.get(next_tier)
                if next_agent and next_tier <= MAX_LOCAL_TIER:
                    _log.info("[TIER] escalating %d→%d due to capability signal", current_tier, next_tier)
                    if event_cb:
                        event_cb({"type": "tier_escalate", "from": current_tier, "to": next_tier})
                    current_tier  = next_tier
                    current_agent = next_agent
                    continue
                elif _t5_available and user_input:
                    # All local tiers exhausted — hand off to Claude Code
                    _log.info("[TIER] escalating %d→T5 (local tiers exhausted)", current_tier)
                    if event_cb:
                        event_cb({"type": "tier_escalate", "from": current_tier, "to": CLAUDE_CODE_TIER,
                                  "reason": "local_exhausted"})
                    return await self._handle_claude_code_direct(
                        user_input, context, trace, event_cb=event_cb
                    )

            # Dry run: report the intended calls and the grants in force, then
            # stop. Deliberately NOT stashed as a pending plan the way task mode
            # does — there is nothing to approve, and leaving an executable plan
            # behind would turn a preview into something one word could fire.
            # Checked before task mode so the two together preview rather than
            # arm a plan.
            if dry_run and iteration == 0:
                _steps = [
                    f"{tc.name}({', '.join(f'{k}={repr(v)[:40]}' for k, v in tc.args.items())})"
                    for tc in (response.tool_calls or [])
                ]
                _al = (grants or {}).get("allowed_tools")
                _mc = (grants or {}).get("mcp_prefixes")
                _lines = ["[DRY RUN — nothing was executed]", ""]
                if _steps:
                    _lines.append("It would call:")
                    _lines += [f"  {i + 1}. {s}" for i, s in enumerate(_steps)]
                else:
                    _lines.append("It would answer directly, calling no tools:")
                    _lines.append("  " + (response.content or "").strip()[:400])
                _lines += [
                    "",
                    "Permitted at this moment:",
                    f"  tools:   {'all' if _al is None else (', '.join(_al) or 'none')}",
                    f"  mcp:     {'all' if _mc is None else (', '.join(_mc) or 'none')}",
                    f"  blocked: {', '.join((grants or {}).get('blocked_tools') or []) or 'none'}",
                    f"  model:   {(grants or {}).get('model') or 'tier selection'}",
                ]
                _log.info("[DRY-RUN] %d intended call(s)", len(_steps))
                return Message(role=Role.ASSISTANT, content=_NL.join(_lines),
                               agent="dry-run")

            if not response.has_tool_calls():
                if response.content:
                    response.content = _sanitize_response(response.content)
                return response

            # Task mode: on first iteration, return the plan instead of executing
            if conv_mode == "task" and iteration == 0:
                plan_steps = [
                    f"{tc.name}({', '.join(f'{k}={repr(v)[:40]}' for k,v in tc.args.items())})"
                    for tc in response.tool_calls
                ]
                plan_text = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(plan_steps))
                self._pending_plans[id(context)] = response.tool_calls
                plan_msg = (
                    f"[TASK MODE] I plan to execute the following steps:\n\n"
                    f"{plan_text}\n\n"
                    f"Reply **approve** to proceed or **cancel** to abort."
                )
                _log.info("[TASK] plan presented: %s", "; ".join(plan_steps))
                return Message(role=Role.ASSISTANT, content=plan_msg, agent=self.primary.name)

            # Persist the assistant's tool-call turn BEFORE the results, so history
            # reads [..., assistant(tool_calls), tool(result)]. Ollama's chat
            # template anchors each tool result to the preceding assistant
            # tool_calls turn; without it the result is an orphan the template
            # drops, and the model re-issues the same call until the iteration cap.
            context.add(response)

            # Emit tool-start events before execution
            if event_cb:
                for tc in response.tool_calls:
                    event_cb({
                        "type":       "tool_start",
                        "name":       tc.name,
                        "args":       {k: str(v)[:120] for k, v in tc.args.items()},
                        "iteration":  iteration,
                    })

            # Execute all tool calls (sequentially to preserve event ordering)
            ts = time.perf_counter()
            tool_results = []
            _judge_on    = _cfg.get("intent_judge", False)
            _judge_model = _cfg.get("intent_judge_model") or _cfg.get("model") or ""
            _guard_on    = _cfg.get("output_guard", False)
            for tc in response.tool_calls:
                _trusted = _approval.is_trusted(user_id, tc.name, tc.args)

                # ── Admin tool policy (approval-time; distinct from RBAC) ──
                # First-match-wins glob+arg rule → allow | deny | ask. Precedence
                # is two-floor: deny/block never de-escalate; allow only collapses
                # the static consequential gate (NOT the judge 'gate' — a steered
                # call can't be silenced by a broad glob).
                _pol = _policy.evaluate(user_role, tc.name, tc.args)
                _pol_action = _pol["action"] if _pol else None
                if _pol_action == "deny":                       # Floor 1
                    reason = _pol.get("reason") or f"rule '{_pol.get('rule')}'"
                    _log.warning("[POLICY] denied %s for role=%s: %s", tc.name, user_role, reason)
                    _approval._audit_log(user_id, tc.name,
                                         _approval._build_summary(tc.name, tc.args), f"policy:deny")
                    tool_results.append((tc, f"Action '{tc.name}' was blocked by policy: {reason}"))
                    if event_cb:
                        event_cb({"type": "tool_end", "name": tc.name,
                                  "result": f"blocked by policy: {reason}"[:200], "ok": False})
                    continue

                # ── Security review (heuristic pre-tier + LLM intent judge) ──
                # The heuristic is sub-ms with no I/O, so it runs regardless of
                # trust/policy-allow → its catastrophic BLOCK (Floor 1) fires even
                # for a trusted or allow-listed call. The LLM judge stays behind
                # `not _trusted` for cost. It may ESCALATE (gate/block), never
                # auto-allow, so the human gate below is never weakened.
                _verdict, judged = None, None
                if _judge_on and not _approval.is_safe(tc.name):
                    judged = _approval.assess_heuristic(tc.name, tc.args)
                    if judged is None and not _trusted:
                        judged = await _approval.assess_intent(
                            user_input, tc.name, tc.args, model=_judge_model
                        )
                    _verdict = judged.get("verdict") if judged else None
                    if judged:
                        _approval._audit_log(
                            user_id, tc.name,
                            _approval._build_summary(tc.name, tc.args), f"judge:{_verdict}"
                        )
                    if _verdict == "block":                     # Floor 1 — unconditional
                        reason = judged.get("reason", "off-intent")
                        _log.warning("[JUDGE] blocked %s for user=%s: %s", tc.name, user_id, reason)
                        tool_results.append((tc, f"Action '{tc.name}' was blocked by the security review: {reason}"))
                        if event_cb:
                            event_cb({"type": "tool_end", "name": tc.name,
                                      "result": f"blocked by security review: {reason}"[:200], "ok": False})
                        continue

                # ── Human approval gate (Floor 2) ──
                # Consequential tools and policy 'ask' are bypassed by an explicit
                # allow (trust rule OR policy-allow). The judge 'gate' is bypassed
                # only by a per-user trust rule, NOT by a broad policy-allow.
                _allow_bypass = _trusted or _pol_action == "allow"
                _need_gate = (
                    # registry= lets MCP tools be classified; without it they are
                    # all treated as safe, which let an MCP shell bypass the gate.
                    ((_approval.is_consequential(tc.name, registry=self.registry)
                      or _pol_action == "ask") and not _allow_bypass)
                    or (_verdict == "gate" and not _trusted)
                )
                if _need_gate:
                    decision = await _approval.request_approval(
                        tc.name, tc.args, user_id=user_id, event_cb=event_cb,
                        risk=(judged if _verdict else None),
                    )
                    if decision != "allow":
                        _log.info("[APPROVAL] denied %s by user=%s", tc.name, user_id)
                        tool_results.append((tc, f"Action '{tc.name}' was denied by the user."))
                        continue

                t_start = time.perf_counter()
                result  = await self.registry.execute(tc.name, tc.args)
                elapsed_ms = round((time.perf_counter() - t_start) * 1000)
                _tool_ok = not str(result).startswith("Error")

                # ── Output guard — sanitize the RESULT before it reaches either
                # the model context or the UI. Redacts secrets, annotates injected
                # instructions in untrusted tool output. Sanitizer, not a gate:
                # errors pass the raw result through (see output_guard.scan). ──
                if _guard_on:
                    _sanitized, _findings = _output_guard.scan(tc.name, result)
                    if _findings:
                        result = _sanitized
                        _fk = ", ".join(sorted({f"{f['kind']}:{f['label']}" for f in _findings}))
                        _log.warning("[OUTPUT_GUARD] %s → %s", tc.name, _fk)
                        _approval._audit_log(user_id, tc.name,
                                             _approval._build_summary(tc.name, tc.args),
                                             f"output_guard:{_fk}"[:80])

                tool_results.append((tc, result))
                # Live agent-behaviour telemetry (self-correction metric)
                _bench_telemetry.record_tool(str(id(context)), tc.name, _tool_ok)
                if event_cb:
                    event_cb({
                        "type":        "tool_end",
                        "name":        tc.name,
                        "result":      str(result)[:200],
                        "elapsed_ms":  elapsed_ms,
                        "ok":          _tool_ok,
                    })
            tool_names = ", ".join(tc.name for tc, _ in tool_results)
            elapsed = time.perf_counter() - ts
            trace.append((f"tool(s): {tool_names}", elapsed, ""))
            # One round = one plan executed; extra rounds = re-planning (plan-stability metric)
            _bench_telemetry.record_round(str(id(context)))

            # Feed results back into context as tool messages
            for tc, result in tool_results:
                context.add(
                    Message(
                        role=Role.TOOL,
                        content=str(result),
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        agent="orchestrator",
                    )
                )

            # ── Escalate on tool-failure loops (behavioural, not prose-based) ──
            # A round with no successful tool call, a tool that keeps erroring, or
            # several unproductive rounds all signal the current tier is out of its
            # depth — escalate rather than let it thrash to the iteration cap.
            # (Denied actions read as non-errors, so user denials don't escalate.)
            _round_ok = any(not str(r).startswith("Error") for _, r in tool_results)
            for _tc, _r in tool_results:
                if str(_r).startswith("Error"):
                    _tool_fail_counts[_tc.name] = _tool_fail_counts.get(_tc.name, 0) + 1
            _rounds_without_success = 0 if _round_ok else _rounds_without_success + 1

            _repeated_tool_error = any(v >= 2 for v in _tool_fail_counts.values())
            _stuck_replanning = _rounds_without_success >= 3
            if _repeated_tool_error or _stuck_replanning:
                _esc_reason = "tool_error_loop" if _repeated_tool_error else "stuck_replanning"
                _esc_detail = (dict(_tool_fail_counts) if _repeated_tool_error
                               else f"{_rounds_without_success} rounds w/o success")
                next_tier  = current_tier + 1
                next_agent = self._tier_agents.get(next_tier)
                if next_agent and next_tier <= MAX_LOCAL_TIER:
                    _log.info("[TIER] escalating %d→%d due to %s (%s)",
                              current_tier, next_tier, _esc_reason, _esc_detail)
                    if event_cb:
                        event_cb({"type": "tier_escalate", "from": current_tier,
                                  "to": next_tier, "reason": _esc_reason})
                    current_tier  = next_tier
                    current_agent = next_agent
                    _tool_fail_counts.clear()
                    _rounds_without_success = 0
                    continue
                elif _t5_available and user_input:
                    _log.info("[TIER] escalating %d→T5 due to %s (local tiers exhausted)",
                              current_tier, _esc_reason)
                    if event_cb:
                        event_cb({"type": "tier_escalate", "from": current_tier,
                                  "to": CLAUDE_CODE_TIER, "reason": _esc_reason})
                    return await self._handle_claude_code_direct(
                        user_input, context, trace, event_cb=event_cb
                    )
                # else: already at the top of what's available — keep trying here

        # Safety: return last response even if loop maxed out
        if response.content:
            response.content = _sanitize_response(response.content)
        return response

    async def _auto_generate_skill(self, user_input: str, response: str,
                                    trace: list) -> None:
        """Background task: ask claude_task to generate a SKILL.md from this task trace.
        Only fires for tasks with ≥5 tool calls. Never blocks the user response."""
        if "claude_task" not in self.registry.names():
            return
        tool_names = [
            lbl.replace("tool(s): ", "")
            for lbl, _, _ in trace if lbl.startswith("tool(s):")
        ]
        if not tool_names:
            return
        prompt = (
            "You just completed a complex task in SUNI. "
            "Generate a SKILL.md document so SUNI can reuse this approach.\n\n"
            f"Task: {user_input[:300]}\n"
            f"Tools used: {', '.join(tool_names)}\n"
            f"Result summary: {response[:400]}\n\n"
            "Call skill_save with:\n"
            "- name: descriptive name for this type of task\n"
            "- description: one sentence what this skill does\n"
            "- category: one of (reporting, web, files, communication, analysis, automation, general)\n"
            "- body: step-by-step markdown procedure with example tool calls\n"
            f"- tool_count: {len(tool_names)}\n\n"
            "Write the skill_save call now. Do not ask for confirmation."
        )
        try:
            await self.registry.execute("claude_task", {"task": prompt, "timeout": 120})
            _log.info("[SKILL] auto-generation triggered for task: %s", user_input[:80])
        except Exception as e:
            _log.warning("[SKILL] auto-generation failed: %s", e)

    def _print_trace(self, trace: list, total: float, user_input: str) -> None:
        short = user_input[:60] + ("…" if len(user_input) > 60 else "")
        console.print(f"\n  [dim]── trace: [italic]{short}[/italic] ──[/dim]")
        for label, elapsed, note in trace:
            bar_w = max(1, int(elapsed / total * 28))
            bar = "█" * bar_w
            pct = elapsed / total * 100
            note_str = f"  [dim]{note}[/dim]" if note else ""
            colour = "red" if elapsed > 5 else "yellow" if elapsed > 2 else "green"
            console.print(
                f"  [dim]{label:<26}[/dim]  [{colour}]{elapsed:5.2f}s[/{colour}]"
                f"  [dim]{bar:<28}[/dim]  [dim]{pct:4.1f}%[/dim]{note_str}"
            )
        console.print(
            f"  [dim]{'TOTAL':<26}[/dim]  [bold]{total:5.2f}s[/bold]\n"
        )

    async def _maybe_prefetch(self, user_input: str, context: Context) -> None:
        """Auto-run web_search for queries that need live data, injecting results
        as context so the model doesn't have to generate the tool call itself."""
        if not _WEB_RE.search(user_input):
            return
        if _SKIP_PREFETCH_RE.search(user_input):
            return
        names = self.registry.names()
        if "web_search" not in names:
            return
        try:
            console.print(f"  [dim]→ auto web_search for live query...[/dim]")
            results = await self.registry.execute("web_search", {"query": user_input})
            context.add(Message(
                role=Role.SYSTEM,
                content=(
                    f"[Live web search results for: {user_input!r}]\n{results}\n\n"
                    "Summarise the relevant information above to answer the user. "
                    "Do not say you cannot access the internet — you just did."
                ),
                agent="orchestrator",
            ))
        except Exception as e:
            console.print(f"  [yellow]web prefetch failed: {e}[/yellow]")

    async def _execute_tool_calls(self, tool_calls) -> list[tuple]:
        tasks = [
            (tc, self.registry.execute(tc.name, tc.args)) for tc in tool_calls
        ]
        results = await asyncio.gather(*[t for _, t in tasks], return_exceptions=True)
        return [(tc, r) for (tc, _), r in zip(tasks, results)]

    @property
    def active_agents(self) -> list[str]:
        return [self.primary.name] + list(self.sub_agents.keys())
