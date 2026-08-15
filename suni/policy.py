"""
Tool-call policies for SUNI — an approval-time layer, distinct from RBAC.

RBAC (rbac.py) is a CAPABILITY layer: it decides which tools a role can even see
(exact names, allow/block, filtered out of the model's tool list). Policies act
LATER, at call time, on a tool the model was allowed to call — deciding
allow / deny / ask based on glob patterns AND argument content, which RBAC can't
express.

A policy list (memory/tool_policies.json, admin-editable) is priority-ordered;
the FIRST matching rule wins. Empty list = no policies = behaviour identical to
before. Each rule:

    {
      "match":       "run_shell",          # fnmatch glob on the tool name
      "arg_pattern": "(?i)\\bprod\\b",      # optional regex over the arg values
      "action":      "deny",               # allow | deny | ask
      "roles":       ["standard"],          # optional; omitted = all roles
      "reason":      "no prod shell for non-admins"
    }

Precedence when consumed by the orchestrator (two floors):
  • Floor 1 — nothing de-escalates: policy `deny` and the heuristic/judge `block`
    always block, even for a trusted or allow-listed call.
  • Floor 2 — explicit allow bypasses: static `consequential`, judge `gate`, and
    policy `ask` require a human gate UNLESS explicitly allowed.
  deny > block > ask/gate/consequential > allow.

`deny` fires at call time (the model still emits the call and wastes a round), so
for an unconditional "this role can't use tool X" prefer RBAC's blocked list —
the tool is never offered. Policy `deny` earns its keep on ARG-CONDITIONAL denies
(e.g. run_shell only when the command mentions prod).
"""
from __future__ import annotations
import json
import logging
import re
from fnmatch import fnmatch
from pathlib import Path

log = logging.getLogger("suni.policy")

_PATH = Path("memory/tool_policies.json")
_ACTIONS = ("allow", "deny", "ask")

# Parsed + compiled cache, invalidated on file mtime.
_cache: list[dict] = []
_cache_mtime: float = -1.0


def _load() -> list[dict]:
    global _cache, _cache_mtime
    try:
        mtime = _PATH.stat().st_mtime
    except OSError:
        # No file → no policies (fast path, no behaviour change).
        _cache, _cache_mtime = [], -1.0
        return _cache
    if mtime == _cache_mtime:
        return _cache
    try:
        raw = json.loads(_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("tool_policies.json must be a JSON array")
        parsed: list[dict] = []
        for i, p in enumerate(raw):
            action = str(p.get("action", "")).lower().strip()
            match  = str(p.get("match", "")).strip()
            if action not in _ACTIONS or not match:
                log.warning("[POLICY] skipping invalid rule #%d (action=%r match=%r)", i, action, match)
                continue
            rx = None
            if p.get("arg_pattern"):
                try:
                    rx = re.compile(p["arg_pattern"])
                except re.error as e:
                    log.warning("[POLICY] rule #%d bad arg_pattern (%s) — treated as name-only", i, e)
            roles = p.get("roles") or None
            parsed.append({
                "match": match, "rx": rx, "action": action,
                "roles": set(roles) if roles else None,
                "reason": str(p.get("reason", "")),
            })
        _cache, _cache_mtime = parsed, mtime
    except Exception as e:
        log.warning("[POLICY] failed to load %s (policies disabled): %s", _PATH, e)
        _cache, _cache_mtime = [], mtime
    return _cache


def evaluate(role: str, tool_name: str, args: dict) -> dict | None:
    """
    First-match-wins policy decision for a proposed tool call. Returns
    {"action": "allow"|"deny"|"ask", "reason": str, "rule": str} or None when no
    rule matches (→ existing behaviour, unchanged).
    """
    try:
        for p in _load():
            if p["roles"] is not None and role not in p["roles"]:
                continue
            if not fnmatch(tool_name, p["match"]):
                continue
            if p["rx"] is not None:
                argstr = " ".join(str(v) for v in args.values())
                if not p["rx"].search(argstr):
                    continue
            return {"action": p["action"], "reason": p["reason"], "rule": p["match"]}
    except Exception as e:                       # a policy bug must never break the tool loop
        log.warning("[POLICY] evaluate error (ignored): %s", e)
    return None


# ── Admin helpers ─────────────────────────────────────────────────────────────

def raw() -> list:
    """Current policy list as stored (for the admin editor)."""
    try:
        if _PATH.exists():
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def save(policies: list) -> None:
    """Validate + persist the policy list. Raises ValueError on a malformed rule."""
    if not isinstance(policies, list):
        raise ValueError("policies must be a list")
    for i, p in enumerate(policies):
        if not isinstance(p, dict):
            raise ValueError(f"rule #{i} must be an object")
        if str(p.get("action", "")).lower() not in _ACTIONS:
            raise ValueError(f"rule #{i}: action must be one of {_ACTIONS}")
        if not str(p.get("match", "")).strip():
            raise ValueError(f"rule #{i}: 'match' is required")
        if p.get("arg_pattern"):
            try:
                re.compile(p["arg_pattern"])
            except re.error as e:
                raise ValueError(f"rule #{i}: invalid arg_pattern regex: {e}")
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(policies, indent=2, ensure_ascii=False), encoding="utf-8")
    global _cache_mtime
    _cache_mtime = -1.0     # force reload on next evaluate
