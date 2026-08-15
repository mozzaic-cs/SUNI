"""
Agent-behaviour suite: tool-calling accuracy + subgoal success.

Objective: the model is offered SUNI's real tool schemas and we compare the
tool_calls it emits against a known-correct answer. No judge.

  tool_accuracy   single-step tasks — did it pick the right tool (+ key arg)?
  subgoal_success multi-step tasks — what fraction of the required tools did
                  it invoke?
"""
from __future__ import annotations
from . import SuiteResult, register

# Single-tool tasks: (prompt, expected_tool, arg_substring|None).
# expected_tool names must exist in SUNI's registry for the call to be scorable;
# they are common tools present for every role that has tool access.
_SINGLE = [
    ("Search the web for today's weather in Lisbon.", "web_search", None),
    ("Send an email to sam@example.com with the subject 'Hi'.", "send_email", "sam@example.com"),
    ("Create a PDF report called report.pdf about our quarterly results.", "create_pdf", None),
    ("Read the file C:/notes/todo.txt and tell me what it says.", "read_file", "todo.txt"),
    ("Download the file at https://example.com/data.zip to my Downloads folder.",
     "download_file", "example.com"),
    ("List the most recent emails in my inbox.", "list_emails", None),
]

# Multi-step tasks: (prompt, [required_tools]).
_MULTI = [
    ("Search the web for the latest AI news and then email a summary to boss@example.com.",
     ["web_search", "send_email"]),
    ("Read the file C:/data/sales.csv and create a PDF summary called sales.pdf.",
     ["read_file", "create_pdf"]),
]

_SYS = ("You are SUNI's tool-using agent. When a task needs a tool, call it. "
        "Choose the most appropriate tool and fill its arguments correctly.")


def _called(tool_calls, name) -> dict | None:
    for tc in tool_calls or []:
        if tc.get("name") == name:
            return tc
    return None


async def run(gen, ctx) -> SuiteResult:
    tools = ctx.get("registry_tools") or []
    progress = ctx.get("progress")
    limit = ctx.get("limit")

    single = _SINGLE[: limit or len(_SINGLE)]
    multi = _MULTI[: limit or len(_MULTI)]

    # ── tool_accuracy (single-step) ──
    acc_pass, details = 0, []
    for i, (prompt, expected, arg_sub) in enumerate(single):
        r = await gen(prompt, system=_SYS, temperature=0.0, seed=7, num_predict=256, tools=tools)
        tcs = r.get("tool_calls") or []
        hit = _called(tcs, expected)
        ok = hit is not None
        if ok and arg_sub:
            argstr = " ".join(str(v) for v in (hit.get("args") or {}).values()).lower()
            ok = arg_sub.lower() in argstr
        acc_pass += ok
        details.append({"expected": expected, "called": [t.get("name") for t in tcs], "ok": ok})
        if progress:
            progress("tool_calling", i + 1, len(single) + len(multi))

    # ── subgoal_success (multi-step) — fraction of required tools invoked ──
    subgoal_fracs = []
    for j, (prompt, required) in enumerate(multi):
        r = await gen(prompt, system=_SYS, temperature=0.0, seed=7, num_predict=384, tools=tools)
        tcs = r.get("tool_calls") or []
        met = sum(1 for t in required if _called(tcs, t))
        subgoal_fracs.append(met / len(required))
        details.append({"required": required, "called": [t.get("name") for t in tcs],
                        "met": met, "of": len(required)})
        if progress:
            progress("tool_calling", len(single) + j + 1, len(single) + len(multi))

    n_single = len(single)
    tool_acc = round(100.0 * acc_pass / n_single, 1) if n_single else None
    subgoal = round(100.0 * sum(subgoal_fracs) / len(subgoal_fracs), 1) if subgoal_fracs else None

    if not tools:
        return SuiteResult(
            suite="tool_calling",
            metrics={"tool_accuracy": None, "subgoal_success": None},
            n=0, passed=0, details=[],
            notes="No tool schemas supplied to the runner — cannot score tool calls.",
            error="no_tools",
        )

    return SuiteResult(
        suite="tool_calling",
        metrics={"tool_accuracy": tool_acc, "subgoal_success": subgoal},
        n=n_single + len(multi), passed=acc_pass, details=details,
        notes="Tool calls compared against known-correct tool from SUNI's real registry.",
    )


register("tool_calling", run)
