"""
invoke_agent — hand a task to a named agent profile, mid-conversation.

This is what makes "ask the Network Admin Agent to check the services" work.
Without it an agent can only be chosen by a human in the UI or named in a
schedule; SUNI itself has no way to delegate.

Two constraints do the real work here, and both exist because delegation is a
new path to the tool layer:

INTERSECTION AGAINST THE CALLER, NOT THE ROLE. The sub-turn runs as the same
user, so the role is the ceiling — but if the CALLING turn is itself already
narrowed by an agent profile, the sub-turn must intersect against that narrowed
set, not the raw role. Otherwise a deliberately restricted agent could delegate
its way back to everything its user can do, which would quietly undo the
non-escalation property the whole agent system rests on.

DEPTH CAP. Agents may be invoked; they may not themselves invoke. A → B → A is
a loop with a GPU and a token budget attached and nothing to stop it. One level
is the boring answer and it is the right one until there is a concrete need for
more.
"""
from __future__ import annotations

from contextvars import ContextVar

# Set by the orchestrator for the duration of a turn. Holds the grants ALREADY
# resolved for this turn, so a nested invocation can intersect against what is
# actually in force rather than re-deriving from the role and losing the
# narrowing the calling agent applied.
CURRENT_GRANTS: ContextVar[dict | None] = ContextVar("suni_current_grants", default=None)
CURRENT_ROLE: ContextVar[str] = ContextVar("suni_current_role", default="standard")
# Depth of agent nesting for this turn. 0 = a human's turn.
AGENT_DEPTH: ContextVar[int] = ContextVar("suni_agent_depth", default=0)

MAX_DEPTH = 1

SCHEMA = {
    "name": "invoke_agent",
    "description": (
        "Hand a task to one of the user's named agents and return its answer. "
        "Use this when the user asks for a specific agent by name, or when a task "
        "clearly belongs to one. The agent answers with its own instructions and "
        "its own restricted set of tools. "
        "Call list_agents first if you are not sure which agents exist — inventing "
        "a name will fail rather than silently doing the work yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "description": "The agent's name or slug, e.g. 'Network Admin Agent' or 'network-admin-agent'",
            },
            "task": {
                "type": "string",
                "description": "What the agent should do, stated in full — it does not see this conversation",
            },
        },
        "required": ["agent", "task"],
    },
}

LIST_SCHEMA = {
    "name": "list_agents",
    "description": (
        "List the named agents available to this user, with what each one is for. "
        "Use before invoke_agent when the user names an agent you do not recognise."
    ),
    "parameters": {"type": "object", "properties": {}},
}

_orchestrator = None


def bind(orchestrator) -> None:
    """Wire the running orchestrator in. Without this the tool reports it is
    unavailable rather than half-working."""
    global _orchestrator
    _orchestrator = orchestrator


def _resolve(name: str, user_id: str, role: str):
    """Find an agent by slug, then by name, case-insensitively.

    Only among those this user may use — an agent nobody told them about must
    not become discoverable by guessing its name through a tool call.
    """
    from .. import agents as _agents
    visible = _agents.list_for_user(user_id, role)
    want = (name or "").strip().lower()
    for a in visible:
        if a["slug"].lower() == want:
            return a
    matches = [a for a in visible if a["name"].strip().lower() == want]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return {"_ambiguous": [a["slug"] for a in matches]}
    loose = [a for a in visible if want and want in a["name"].strip().lower()]
    if len(loose) == 1:
        return loose[0]
    if len(loose) > 1:
        return {"_ambiguous": [a["slug"] for a in loose]}
    return None


async def list_handler(**_kwargs) -> str:
    from ..tools.registry import USER_ID_CTX
    from .. import agents as _agents
    uid = USER_ID_CTX.get("")
    role = CURRENT_ROLE.get("standard")
    rows = [a for a in _agents.list_for_user(uid, role) if a.get("enabled", True)]
    if not rows:
        return "No agents are defined. SUNI answers as itself."
    out = ["Agents available to you:"]
    for a in rows:
        out.append(f"- {a['name']} (slug: {a['slug']}) — {a.get('description') or 'no description'}")
    return "\n".join(out)


async def handler(agent: str = "", task: str = "", **_kwargs) -> str:
    from ..tools.registry import USER_ID_CTX
    from .. import agents as _agents

    if _orchestrator is None:
        return "Agent delegation is unavailable (no orchestrator bound)."
    if not agent or not task:
        return "invoke_agent needs both 'agent' and 'task'."

    depth = AGENT_DEPTH.get(0)
    if depth >= MAX_DEPTH:
        return (f"Refused: agents cannot invoke other agents (depth limit {MAX_DEPTH}). "
                f"Do the work directly, or tell the user which agent they should call.")

    uid = USER_ID_CTX.get("")
    role = CURRENT_ROLE.get("standard")
    found = _resolve(agent, uid, role)
    if found is None:
        return (f"There is no agent called {agent!r} available to you. "
                f"Use list_agents to see what exists — do not substitute your own work "
                f"for the agent the user asked for without saying so.")
    if "_ambiguous" in found:
        return (f"{agent!r} matches more than one agent: {', '.join(found['_ambiguous'])}. "
                f"Ask which one is meant.")
    if not found.get("enabled", True):
        return f"The agent {found['name']!r} is disabled."

    profile = _agents.get(found["slug"])
    if not profile:
        return f"The agent {found['name']!r} could not be loaded."

    # Narrow the profile by what is ALREADY in force for this turn. Passing the
    # profile alone would re-derive grants from the role and discard any
    # narrowing the calling agent applied.
    caller = CURRENT_GRANTS.get(None)
    if caller:
        c_tools = caller.get("allowed_tools")
        if c_tools is not None:
            declared = profile.get("tools")
            profile["tools"] = (list(c_tools) if declared is None
                                else [t for t in declared if t in set(c_tools)])
        profile["blocked"] = sorted(set(profile.get("blocked") or [])
                                    | set(caller.get("blocked_tools") or []))
        c_mcp = caller.get("mcp_prefixes")
        if c_mcp is not None:
            d_mcp = profile.get("mcp_servers")
            profile["mcp_servers"] = (list(c_mcp) if d_mcp is None
                                      else [p for p in d_mcp if p in set(c_mcp)])

    from ..core.context import Context as _Ctx
    depth_token = AGENT_DEPTH.set(depth + 1)
    try:
        sub = _Ctx()
        answer = await _orchestrator._safe_run(
            task, sub,
            user_role=role,
            user_id=uid,
            agent_profile=profile,
        )
    except Exception as exc:      # noqa: BLE001 — surface it, do not answer instead
        return f"The agent {found['name']!r} failed: {exc}"
    finally:
        AGENT_DEPTH.reset(depth_token)

    return f"[{found['name']}]\n{answer}"
