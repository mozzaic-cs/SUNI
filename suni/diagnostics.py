"""Why is SUNI unwell — answered by SUNI, from her own evidence.

Written after an afternoon spent watching a Telegram message vanish. The answer
was in the logs the whole time: a turn logged and never completed, a model
evicted for want of VRAM, a tool schema larger than the context window it was
sent through. Every one of those was a `tail` and a `grep` away, and none of
them was reachable by the person whose machine it was.

So this reads what is already there — SUNI's own log, Ollama's server log, the
run state, the live config — and says what is wrong in sentences. It is not a
monitoring system and it does not watch anything: it is a question you ask when
something feels broken, and it answers from evidence with the line numbers to
check.

THREE RULES, all of them learned the hard way in this codebase:

  It only READS. No restarts, no config writes, no model loads. A diagnosis
  that changes the thing it is diagnosing is not a diagnosis.

  It NEVER returns a secret. Logs hold connection strings and error text from
  libraries that embed credentials in their messages; a bot token reached this
  repo inside a screenshot only hours before this file was written. Everything
  that leaves here goes through the same scrubber log shipping uses.

  It says what it CHECKED, including the checks that found nothing. A report
  listing only problems cannot be told apart from a report that failed to look.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Severity, in the order a reader should meet them.
BROKEN, SLOW, NOTE, OK = "broken", "slow", "note", "ok"
_ORDER = {BROKEN: 0, SLOW: 1, NOTE: 2, OK: 3}


class Finding:
    __slots__ = ("level", "what", "evidence", "fix")

    def __init__(self, level: str, what: str, evidence: str = "", fix: str = ""):
        self.level, self.what, self.evidence, self.fix = level, what, evidence, fix

    def as_text(self) -> str:
        mark = {BROKEN: "BROKEN", SLOW: "SLOW  ", NOTE: "NOTE  ", OK: "ok    "}[self.level]
        out = f"  [{mark}] {self.what}"
        if self.evidence:
            out += f"\n           evidence: {self.evidence}"
        if self.fix:
            out += f"\n           try: {self.fix}"
        return out


def _scrub(text: Any) -> str:
    """Nothing leaves here with a credential in it."""
    try:
        from .log_ship import _safe
        return _safe(text)
    except Exception:      # noqa: BLE001 — never fail a diagnosis on the scrubber
        s = str(text)
        s = re.sub(r"(?i)\b(pass(word)?|token|secret|api[_-]?key)\b\s*[=:]\s*\S+",
                   r"\1=***", s)
        return s


def _tail(path: Path, n: int = 400, window: int = 400_000) -> list[str]:
    """The last n lines, without reading the whole file to get them.

    `window` is how many BYTES back to reach for them, and it matters more than
    n does: Ollama's server log is thirty megabytes and so chatty that four
    hundred kilobytes covers only a few minutes. The first version of this
    check read that much, found no evictions, and reported the backend healthy
    minutes after one — a check that silently looked at nothing, which is the
    exact failure this file exists to catch.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - window))
            data = fh.read().decode("utf-8", "replace")
        return data.splitlines()[-n:]
    except Exception:      # noqa: BLE001
        return []


# ── the checks ───────────────────────────────────────────────────────────────

def _check_turns(lines: list[str]) -> list[Finding]:
    """Did the turns that started actually finish?

    This is the check that would have answered the whole afternoon. A [REQUEST]
    with no [DONE] after it is a turn that went in and never came out, and it
    looks like nothing at all from the outside — no error, no reply, silence.
    """
    out: list[Finding] = []
    started, finished = [], 0
    slowest = 0.0
    for ln in lines:
        if "[REQUEST]" in ln:
            started.append(ln)
        elif "[DONE]" in ln:
            finished += 1
            if started:
                started.pop()
            m = re.search(r"total=([\d.]+)s", ln)
            if m:
                slowest = max(slowest, float(m.group(1)))
    if started:
        last = _scrub(started[-1])[:150]
        out.append(Finding(
            BROKEN,
            f"{len(started)} turn(s) began and never finished",
            last,
            "A turn with no [DONE] is stuck in the model call. Check the backend "
            "below; a request with no timeout used to hang for ever here."))
    else:
        out.append(Finding(OK, f"every turn that started also finished ({finished} recently)"))
    if slowest > 60:
        out.append(Finding(
            SLOW, f"slowest recent turn took {slowest:.0f}s",
            "", "See the prompt/gen breakdown on its [DONE] line: a large "
                "prompt figure is tool schemas, a long load is a cold model."))
    return out


def _check_errors(lines: list[str]) -> list[Finding]:
    bad = [ln for ln in lines
           if ("| ERROR" in ln or "Traceback" in ln or "| WARNING" in ln)
           and "client disconnects filtered" not in ln]
    if not bad:
        return [Finding(OK, "no errors or warnings in the recent log")]
    worst = [ln for ln in bad if "| ERROR" in ln or "Traceback" in ln]
    lvl = BROKEN if worst else NOTE
    sample = _scrub((worst or bad)[-1])[:170]
    return [Finding(lvl, f"{len(bad)} warning(s)/error(s) in the recent log", sample)]


def _check_backend() -> list[Finding]:
    """Is the local model backend actually able to answer?

    Ollama's own server log is the only honest source for this. `ollama ps`
    reports "100% GPU" for a model it is about to evict, which is how an
    afternoon gets spent on the wrong hypothesis.
    """
    out: list[Finding] = []
    log = Path(os.environ.get("LOCALAPPDATA", "")) / "Ollama" / "server.log"
    if not log.exists():
        log = Path.home() / ".ollama" / "logs" / "server.log"
    if not log.exists():
        return [Finding(NOTE, "no Ollama server log found — cannot check the backend",
                        str(log))]
    # Four megabytes and fifty thousand lines: enough of this log to cover the
    # last hour or so of a busy machine.
    lines = _tail(log, 50_000, window=4_000_000)
    span = _window_of(lines)
    evict = [ln for ln in lines if "evicting" in ln.lower()]
    if evict:
        out.append(Finding(
            SLOW,
            f"the model backend evicted a model {len(evict)} time(s) recently",
            _scrub(evict[-1])[:160],
            "Two models plus the desktop do not fit the card, so each turn pays "
            "a cold load. Free VRAM (close 3D/WebGL pages) or move the embedding "
            "model to a second GPU."))
    offload = [ln for ln in lines if "offloaded" in ln and "layers to GPU" in ln]
    if offload:
        m = re.search(r"offloaded (\d+)/(\d+) layers", offload[-1])
        if m and m.group(1) != m.group(2):
            out.append(Finding(
                SLOW, f"the model is only partly on the GPU ({m.group(0)})", "",
                "Layers on the CPU are very slow on a machine without AVX2. "
                "Free VRAM or use a smaller model."))
        elif m:
            out.append(Finding(OK, f"the model is fully on the GPU ({m.group(0)})"))
    if not out:
        # Say what was looked at, or "nothing wrong" cannot be told from
        # "nothing read".
        out.append(Finding(OK, f"the model backend log shows nothing wrong{span}"))
    return out


def _window_of(lines: list[str]) -> str:
    """The time range those lines actually cover, for the report to admit to."""
    stamps = [m.group(1) for ln in lines
              if (m := re.search(r"time=(\d{4}-\d\d-\d\dT[\d:]{8})", ln))]
    if not stamps:
        return ""
    return f" (covering {stamps[0][11:]}–{stamps[-1][11:]})"


def _check_config() -> list[Finding]:
    """Settings that are individually fine and together are a problem."""
    out: list[Finding] = []
    try:
        from . import config as _cfg
        from .system_profile import effective_num_ctx
    except Exception as exc:      # noqa: BLE001
        return [Finding(NOTE, f"could not read the configuration: {_scrub(exc)}")]

    ctx = effective_num_ctx()
    try:
        import json
        import importlib
        import pkgutil
        from . import rbac
        from . import tools as _T
        blocked = set(rbac.blocked_tools("standard"))
        tok = 0
        for m in pkgutil.iter_modules(_T.__path__):
            try:
                mod = importlib.import_module("suni.tools." + m.name)
            except Exception:      # noqa: BLE001
                continue
            for a in dir(mod):
                if a == "SCHEMA" or a.endswith("_SCHEMA"):
                    s = getattr(mod, a)
                    if isinstance(s, dict) and "name" in s and s["name"] not in blocked:
                        tok += len(json.dumps(s)) // 4
        share = tok / max(ctx, 1)
        ratio = float(_cfg.get("tool_budget_ratio", 0.45) or 0.45)
        if share <= ratio:
            out.append(Finding(OK, f"every tool fits the window "
                                   f"({tok} tokens of {ctx}, {share:.0%})"))
        else:
            # The budget stops this being fatal; it does not stop it being a
            # fact worth knowing, because the tools that do not fit are cut.
            out.append(Finding(
                NOTE,
                f"the full tool set is {share:.0%} of the context window "
                f"({tok} tokens of {ctx}) and is trimmed to fit",
                "", "The budget keeps the prompt whole, but the model is only "
                    f"offered what fits in {ratio:.0%}. More context, or fewer "
                    "tools for this role, means fewer cuts."))
    except Exception:      # noqa: BLE001 — a check that cannot run is not a failure
        pass

    if not _cfg.get("ollama_timeout_s"):
        out.append(Finding(
            BROKEN, "the model client has no request timeout", "",
            "Without one a hung generation waits for ever and takes the turn "
            "with it, and the circuit breaker never sees a failure to count."))
    return out


def _check_channels() -> list[Finding]:
    """Enabled, configured, and allowed to talk to anyone — three separate things."""
    out: list[Finding] = []
    try:
        from . import config as _cfg
    except Exception:      # noqa: BLE001
        return []
    for name, allow_key in (("telegram", "telegram_allowed_chats"),
                            ("discord", "discord_allowed_channels"),
                            ("slack", "slack_allowed_channels")):
        if not _cfg.get(f"{name}_enabled"):
            continue
        allowed = _cfg.get(allow_key) or []
        if not allowed:
            out.append(Finding(
                NOTE, f"{name} is on but its allow-list is empty",
                "", f"Nobody can talk to it yet — that is the fail-closed state. "
                    f"Message it once and it replies with the id to add."))
        else:
            out.append(Finding(OK, f"{name} is on with {len(allowed)} allow-listed"))
    return out or [Finding(NOTE, "no messaging channels are enabled")]


def _check_runstate() -> list[Finding]:
    try:
        from . import runstate
        prev = runstate.previous_run() if hasattr(runstate, "previous_run") else None
        if prev and not prev.get("clean", True):
            return [Finding(NOTE, "the previous run ended without a clean shutdown", "",
                            "It was killed rather than stopped — check the host's "
                            "service manager if this repeats.")]
    except Exception:      # noqa: BLE001
        pass
    return []


def run(log_path: str = "") -> dict:
    """Everything, as findings. The caller decides how to present it."""
    t0 = time.perf_counter()
    findings: list[Finding] = []
    checked: list[str] = []

    path = Path(log_path) if log_path else None
    if path is None:
        try:
            from .logger import current_log_path
            path = current_log_path()
        except Exception:      # noqa: BLE001
            path = None
    lines = _tail(path) if path and path.exists() else []
    if lines:
        checked.append(f"the last {len(lines)} lines of {path.name}")
        findings += _check_turns(lines)
        findings += _check_errors(lines)
    else:
        findings.append(Finding(NOTE, "no application log was readable",
                                str(path or "—")))

    for label, fn in (("the model backend's own log", _check_backend),
                      ("the live configuration", _check_config),
                      ("the messaging channels", _check_channels),
                      ("the previous shutdown", _check_runstate)):
        checked.append(label)
        try:
            findings += fn()
        except Exception as exc:      # noqa: BLE001 — one broken check is not a broken report
            findings.append(Finding(NOTE, f"the {label} check could not run",
                                    _scrub(exc)[:120]))

    findings.sort(key=lambda f: _ORDER[f.level])
    return {
        "findings": findings,
        "checked": checked,
        "took_s": round(time.perf_counter() - t0, 2),
    }


def as_report(result: dict) -> str:
    fs: list[Finding] = result["findings"]
    bad = [f for f in fs if f.level in (BROKEN, SLOW)]
    head = ("Everything I can check looks healthy."
            if not bad else
            f"{len(bad)} thing(s) look wrong. Worst first:")
    body = [f.as_text() for f in fs]
    tail = ("\nI looked at: " + "; ".join(result["checked"]) + "."
            + f" ({result['took_s']}s)")
    return head + "\n" + "\n".join(body) + "\n" + tail
